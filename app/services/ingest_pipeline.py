"""入库流水线：5 阶段并发，ASR 受 Semaphore 限制。

将串行入库改为 asyncio.Queue 生产-消费模型：

    download → transcode → asr → embedding → done

每阶段一个或多个 consumer 协程，任务在阶段间通过 queue 流转，实现多视频
流水线并发（视频 A 在 asr 时，视频 B 可在 download）。ASR 阶段额外用
Semaphore 限流，避免并发 ASR 打爆 GPU/CPU 或上游 API 配额；其他阶段不限流。

设计要点：
- ASR 阶段启动 ``max_asr_concurrency`` 个 consumer，使 ASR 可并发执行；
  每个 consumer 在执行 handler 前获取 ``self._asr_sem``，确保 ASR 并发数
  严格不超过 ``max_asr_concurrency``。其余阶段各启动 1 个 consumer。
- 失败隔离：任意阶段抛异常 → ``mark_failed`` 记录错误，任务停止流转，
  不影响其他任务。
- 断点续传：每次阶段流转调用 ``ingest_task_store.update_stage`` 持久化进度，
  完成调用 ``mark_done``，与现有 IngestTask 表对接。

注：本模块为可选增强，不破坏现有单视频入库（``data_syncer.ingest_local_audio_file``
与 ``knowledge._ingest_single_video`` 保持原逻辑）。需要流水线并发的调用方可
通过 :func:`get_pipeline` 获取单例并 :meth:`submit` 任务。
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from app.config import settings
from app.services.ingest_task_store import mark_done, mark_failed, update_stage


# 5 阶段顺序：download → transcode → asr → embedding → done
# done 为终态，不需要对应 queue
_STAGES: List[str] = ["download", "transcode", "asr", "embedding", "done"]


# 阶段处理器签名：(task) -> None
# 处理器负责执行该阶段的具体逻辑，可读写 task.state 携带阶段间数据。
StageHandler = Callable[["PipelineTask"], Awaitable[None]]


@dataclass
class PipelineTask:
    """流水线任务对象，携带入库任务上下文与阶段间状态。

    Attributes:
        id: IngestTask.id，用于持久化阶段进度（update_stage/mark_failed/mark_done）
        video_id: 视频 ID（bvid / aweme_id / 本地文件 uuid hex）
        platform: bilibili / douyin / local
        db: 异步会话，用于阶段进度持久化。单个任务在阶段间顺序流转
            （同一时刻只处于一个阶段），因此一个任务持有一个会话是安全的；
            不同任务持有不同会话，互不干扰。
        payload: 任务参数（file_path / url / title 等），由调用方填充
        state: 阶段间传递的可变状态（downloaded_path / asr_text / chunks 等）
    """

    id: int
    video_id: str
    platform: str
    db: Any  # sqlalchemy.ext.asyncio.AsyncSession
    payload: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)


class IngestPipeline:
    """asyncio.Queue 生产-消费模型，多视频流水线并发。

    Args:
        max_asr_concurrency: ASR 阶段并发上限（同时执行的 ASR 数）
        stage_handlers: 可选的自定义阶段处理器映射，覆盖默认本地文件流程。
            键为阶段名（download/transcode/asr/embedding），值为 async handler。
            主要供测试注入 mock，生产环境使用默认处理器。
    """

    def __init__(
        self,
        max_asr_concurrency: int = 2,
        stage_handlers: Optional[Dict[str, StageHandler]] = None,
    ):
        self._max_asr_concurrency = max_asr_concurrency
        self._asr_sem = asyncio.Semaphore(max_asr_concurrency)
        # done 不需要 queue
        self._queues: Dict[str, asyncio.Queue] = {
            stage: asyncio.Queue() for stage in _STAGES[:-1]
        }
        self._consumers: List[asyncio.Task] = []
        self._running = False
        # 阶段处理器：默认使用本地文件流程；可被 stage_handlers 覆盖
        self._handlers: Dict[str, StageHandler] = dict(
            stage_handlers or _default_stage_handlers()
        )

    async def start(self) -> None:
        """启动各阶段 consumer。

        - download / transcode / embedding：各 1 个 consumer
        - asr：max_asr_concurrency 个 consumer，配合 Semaphore 使 ASR 可并发
          执行但并发数严格不超过上限
        """
        if self._running:
            return
        self._running = True
        consumers: List[asyncio.Task] = []
        for stage in _STAGES[:-1]:
            if stage == "asr":
                # 多 consumer 让 ASR 可并发；Semaphore 保证并发数不超过上限
                for _ in range(max(self._max_asr_concurrency, 1)):
                    consumers.append(asyncio.create_task(self._consume("asr")))
            else:
                consumers.append(asyncio.create_task(self._consume(stage)))
        self._consumers = consumers
        logger.info(
            f"[Pipeline] 已启动 {len(consumers)} 个 consumer"
            f"（ASR 并发上限={self._max_asr_concurrency}）"
        )

    async def stop(self) -> None:
        """停止所有 consumer：置 running=False、投递哨兵、取消任务。"""
        self._running = False
        for q in self._queues.values():
            await q.put(None)  # 哨兵，让阻塞在 get() 的 consumer 尽快退出
        for c in self._consumers:
            c.cancel()
        # 等待取消完成，忽略 CancelledError
        for c in self._consumers:
            try:
                await c
            except (asyncio.CancelledError, Exception):
                pass
        self._consumers = []
        logger.info("[Pipeline] 已停止所有阶段 consumer")

    async def submit(self, task: PipelineTask) -> None:
        """提交一个入库任务到 download 阶段 queue。"""
        await self._queues["download"].put(task)

    async def _consume(self, stage: str) -> None:
        """单阶段 consumer：取任务 → 处理 → 推入下一阶段。

        收到 None 哨兵即退出（stop() 投递）。被 cancel 时退出。
        """
        queue = self._queues[stage]
        while self._running:
            try:
                task = await queue.get()
            except asyncio.CancelledError:
                return
            if task is None:
                # 哨兵：停止信号
                break
            await self._process_one(stage, task)

    async def _process_one(self, stage: str, task: PipelineTask) -> None:
        """处理单个任务的一个阶段：限流 → 执行 handler → 流转 / 收尾。

        失败时 mark_failed 并停止该任务流转，不影响其他任务。
        """
        try:
            handler = self._handlers.get(stage)
            if handler is not None:
                if stage == "asr":
                    # ASR 阶段用 Semaphore 限流，避免并发打爆资源
                    async with self._asr_sem:
                        await handler(task)
                else:
                    await handler(task)

            next_stage = _STAGES[_STAGES.index(stage) + 1]
            if next_stage == "done":
                await self._mark_done(task)
            else:
                await self._update_stage(task, next_stage)
                await self._queues[next_stage].put(task)
        except asyncio.CancelledError:
            # 取消不是失败，向上抛出让 consumer 退出
            raise
        except Exception as e:
            logger.warning(
                f"[Pipeline] stage={stage} task={task.id} "
                f"[{task.platform}/{task.video_id}] failed: {e}"
            )
            await self._mark_failed(task, str(e))

    # ---- 持久化辅助：每个操作独立 commit，失败仅记录日志不抛出 ----
    async def _update_stage(self, task: PipelineTask, stage: str) -> None:
        try:
            await update_stage(task.db, task.id, stage, status="running")
            await task.db.commit()
        except Exception as e:
            logger.warning(
                f"[Pipeline] update_stage 失败 task={task.id} stage={stage}: {e}"
            )

    async def _mark_done(self, task: PipelineTask) -> None:
        try:
            await mark_done(task.db, task.id)
            await task.db.commit()
        except Exception as e:
            logger.warning(f"[Pipeline] mark_done 失败 task={task.id}: {e}")

    async def _mark_failed(self, task: PipelineTask, error: str) -> None:
        try:
            await mark_failed(task.db, task.id, error)
            await task.db.commit()
        except Exception as e:
            logger.warning(f"[Pipeline] mark_failed 失败 task={task.id}: {e}")


# ===========================================================================
# 默认阶段处理器：本地文件流程
# 复用 data_syncer.ingest_local_audio_file 的内部步骤，拆分为独立阶段以支持
# 流水线并发。B站/抖音流程因需要 session 上下文，暂不接入流水线，保持原有
# _ingest_single_video 逻辑。
# ===========================================================================


async def _default_download(task: PipelineTask) -> None:
    """download 阶段：本地文件无需下载，记录文件路径供后续阶段使用。"""
    task.state["file_path"] = task.payload.get("file_path")


async def _default_transcode(task: PipelineTask) -> None:
    """transcode 阶段：本地文件默认无需转码，直通。

    预留：若后续需要转码（如视频→音频），在此实现。
    """
    return


async def _default_asr(task: PipelineTask) -> None:
    """asr 阶段：调用 ASRService 转写本地文件，结果存入 task.state。"""
    from app.services.asr import ASRService

    file_path = task.state.get("file_path") or task.payload.get("file_path")
    if not file_path:
        raise RuntimeError("ASR 阶段缺少 file_path")
    asr = ASRService()
    text = await asr.transcribe_local_file(file_path)
    if not text or len(text.strip()) < 20:
        raise RuntimeError(f"ASR 转写失败或内容过少: {task.video_id}")
    task.state["asr_text"] = text


async def _default_embedding(task: PipelineTask) -> None:
    """embedding 阶段：切片 + 向量化，写入向量库与 VideoCache。"""
    from app.models import VideoCache, VideoContent, ContentSource
    from app.services.rag import RAGService

    text = task.state.get("asr_text")
    if not text:
        raise RuntimeError("embedding 阶段缺少 asr_text")
    bvid = task.video_id
    title = (
        task.payload.get("title")
        or task.payload.get("original_filename")
        or "本地文件"
    )
    content = VideoContent(
        bvid=bvid,
        title=title,
        content=text,
        source=ContentSource.ASR,
        platform="local",
        description=task.payload.get("original_filename"),
    )
    # 本地文件使用独立的向量集合，避免与 B站/抖音数据混淆
    rag = RAGService(collection_name="local_files")
    # 清理可能存在的旧向量（保险起见）
    try:
        await asyncio.to_thread(rag.delete_video, bvid)
    except Exception:
        pass
    chunks = await asyncio.to_thread(rag.add_video_content, content)
    if chunks <= 0:
        raise RuntimeError("未生成可写入的向量文档")
    # 写入 VideoCache 以便后续查询与管理
    try:
        task.db.add(
            VideoCache(
                bvid=bvid,
                platform="local",
                title=title,
                content=text,
                content_source=ContentSource.ASR.value,
                description=task.payload.get("original_filename"),
                is_processed=True,
            )
        )
        await task.db.commit()
    except Exception as e:
        logger.warning(f"[Pipeline] 写入 VideoCache 失败 [{bvid}]: {e}")
    task.state["chunks"] = chunks


def _default_stage_handlers() -> Dict[str, StageHandler]:
    """返回默认阶段处理器映射（本地文件流程）。"""
    return {
        "download": _default_download,
        "transcode": _default_transcode,
        "asr": _default_asr,
        "embedding": _default_embedding,
    }


# ===========================================================================
# 模块级单例：lifespan startup 创建并启动，shutdown 停止
# ===========================================================================
_pipeline: Optional[IngestPipeline] = None


async def get_pipeline() -> IngestPipeline:
    """获取流水线单例（首次调用时按 settings.max_asr_concurrency 创建并启动）。"""
    global _pipeline
    if _pipeline is None:
        _pipeline = IngestPipeline(settings.max_asr_concurrency)
        await _pipeline.start()
    return _pipeline


async def shutdown_pipeline() -> None:
    """关闭流水线单例（lifespan shutdown 调用）。"""
    global _pipeline
    if _pipeline is not None:
        await _pipeline.stop()
        _pipeline = None
