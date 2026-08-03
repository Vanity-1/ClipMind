"""数据同步校验服务 - 确保 ChromaDB 与 SQLite 数据一致性"""

import asyncio
from loguru import logger
from typing import Optional
from datetime import datetime, timedelta
from enum import Enum
from sqlalchemy import select

from app.models import utcnow
from app.services.retry import with_retry


class SyncStatus(str, Enum):
    CONSISTENT = "consistent"
    VECTOR_MISSING = "vector_missing"
    VECTOR_ORPHAN = "vector_orphan"
    VECTOR_COUNT_MISMATCH = "vector_count_mismatch"
    NEEDS_REPROCESSING = "needs_reprocessing"


class SyncReport:
    def __init__(self):
        self.total_checked = 0
        self.consistent = 0
        self.vector_missing = 0
        self.vector_orphan = 0
        self.vector_count_mismatch = 0
        self.needs_reprocessing = 0
        self.errors: list[str] = []
        self.details: list[dict] = []

    def to_dict(self) -> dict:
        return {
            "total_checked": self.total_checked,
            "consistent": self.consistent,
            "vector_missing": self.vector_missing,
            "vector_orphan": self.vector_orphan,
            "vector_count_mismatch": self.vector_count_mismatch,
            "needs_reprocessing": self.needs_reprocessing,
            "errors": self.errors,
            "details": self.details[:100],
        }


class DataSyncer:
    """数据同步校验器"""

    def __init__(self, db_session_factory, rag_service, batch_size: int = 50):
        self.db_factory = db_session_factory
        self.rag = rag_service
        self.batch_size = batch_size

    async def check_consistency(self, since_hours: Optional[int] = None) -> SyncReport:
        """
        检查数据一致性
        since_hours: 只检查最近N小时内更新的视频，None表示全部
        """
        report = SyncReport()

        try:
            async with self.db_factory() as db:
                from app.models import VideoCache
                stmt = select(VideoCache).where(VideoCache.is_processed.is_(True))
                if since_hours:
                    cutoff = utcnow() - timedelta(hours=since_hours)
                    stmt = stmt.where(VideoCache.updated_at >= cutoff)
                result = await db.execute(stmt)
                processed_videos = result.scalars().all()

                for video in processed_videos:
                    report.total_checked += 1
                    try:
                        result = await self._check_single_video(video)
                        self._update_report(report, video, result)
                    except Exception as e:
                        report.errors.append(f"[{video.bvid}] 校验异常: {e}")
                        report.needs_reprocessing += 1

                await self._check_orphan_vectors(db, report)

        except Exception as e:
            logger.error(f"数据同步校验失败: {e}")
            report.errors.append(f"整体校验异常: {e}")

        return report

    async def _check_single_video(self, video) -> SyncStatus:
        """检查单个视频的数据一致性"""
        has_vectors = await asyncio.to_thread(self.rag.has_video, video.bvid)

        if not has_vectors:
            if video.is_processed:
                return SyncStatus.VECTOR_MISSING
            return SyncStatus.CONSISTENT

        chunk_count = await asyncio.to_thread(self.rag.count_video_chunks, video.bvid)
        expected_min = getattr(video, 'chunk_count', 0) or 0

        if chunk_count == 0 and has_vectors:
            return SyncStatus.VECTOR_COUNT_MISMATCH

        if expected_min > 0 and chunk_count < expected_min:
            return SyncStatus.VECTOR_COUNT_MISMATCH

        return SyncStatus.CONSISTENT

    def _update_report(self, report: SyncReport, video, status: SyncStatus):
        if status == SyncStatus.CONSISTENT:
            report.consistent += 1
        elif status == SyncStatus.VECTOR_MISSING:
            report.vector_missing += 1
            report.needs_reprocessing += 1
            report.details.append({
                "bvid": video.bvid,
                "title": video.title,
                "status": status.value,
                "action": "需要重新向量化",
            })
        elif status == SyncStatus.VECTOR_COUNT_MISMATCH:
            report.vector_count_mismatch += 1
            report.needs_reprocessing += 1
            report.details.append({
                "bvid": video.bvid,
                "title": video.title,
                "status": status.value,
                "action": "需要重新处理",
            })

    async def _check_orphan_vectors(self, db, report: SyncReport):
        """检查向量库中是否存在孤儿数据"""
        try:
            all_vector_bvids = await asyncio.to_thread(self.rag.get_all_video_ids)
            if not all_vector_bvids:
                return

            from app.models import VideoCache
            for bvid in all_vector_bvids:
                result = await db.execute(select(VideoCache).where(VideoCache.bvid == bvid))
                video = result.scalar_one_or_none()
                if not video or not video.is_processed:
                    report.vector_orphan += 1
                    report.details.append({
                        "bvid": bvid,
                        "status": SyncStatus.VECTOR_ORPHAN.value,
                        "action": "清理孤儿向量",
                    })
        except Exception as e:
            report.errors.append(f"孤儿向量检查异常: {e}")

    async def cleanup_orphan_vectors(self, bvids: list[str]) -> dict:
        """清理孤儿向量"""
        results = {"cleaned": 0, "errors": []}
        for bvid in bvids:
            try:
                await asyncio.to_thread(self.rag.delete_video, bvid)
                results["cleaned"] += 1
                logger.info(f"已清理孤儿向量: {bvid}")
            except Exception as e:
                results["errors"].append(f"{bvid}: {e}")
        return results

    async def schedule_sync_check(self, interval_hours: int = 6):
        """定期同步检查（可作为后台任务）。

        每次检查前先处理 pending_cleanup 表中尚未清理的记录，
        避免孤儿向量留到下一个周期才被发现。
        """
        while True:
            try:
                # 1. 先处理待清理记录
                cleanup_report = await self.process_pending_cleanup()
                if cleanup_report["pending"] > 0:
                    logger.info(f"pending_cleanup 处理: {cleanup_report}")

                # 2. 执行正常一致性检查
                logger.info("开始数据同步检查...")
                report = await self.check_consistency(since_hours=interval_hours * 2)
                logger.info(f"同步检查完成: {report.to_dict()}")

                orphan_bvids = [d["bvid"] for d in report.details
                                if d["status"] == SyncStatus.VECTOR_ORPHAN.value]
                if orphan_bvids:
                    cleanup_result = await self.cleanup_orphan_vectors(orphan_bvids)
                    logger.info(f"孤儿向量清理: {cleanup_result}")

            except Exception as e:
                logger.error(f"定期同步检查异常: {e}")

            await asyncio.sleep(interval_hours * 3600)

    async def process_pending_cleanup(self) -> dict:
        """处理 pending_cleanup 表中未清理的记录。

        Returns:
            {"pending": 读取到的待处理条数, "cleaned": 成功清理条数, "errors": [...]}
        """
        result = {"pending": 0, "cleaned": 0, "errors": []}
        try:
            async with self.db_factory() as db:
                from app.models import PendingCleanup
                from sqlalchemy import select, update

                stmt = (
                    select(PendingCleanup)
                    .where(PendingCleanup.cleaned.is_(False))
                    .order_by(PendingCleanup.created_at.asc())
                    .limit(500)
                )
                rows = (await db.execute(stmt)).scalars().all()
                result["pending"] = len(rows)

                for row in rows:
                    try:
                        vector_ids = row.vector_ids_json or []
                        if vector_ids:
                            await asyncio.to_thread(
                                self.rag.vectorstore._collection.delete, ids=list(vector_ids)
                            )
                        # 也按 bvid 兜底删除（避免 vector_ids 不全）
                        try:
                            await asyncio.to_thread(self.rag.delete_video, row.bvid)
                        except Exception:
                            pass
                        row.cleaned = True
                        row.cleaned_at = utcnow()
                        result["cleaned"] += 1
                    except Exception as e:
                        result["errors"].append(f"#{row.id} bvid={row.bvid}: {e}")

                await db.commit()
        except Exception as e:
            result["errors"].append(f"process_pending_cleanup 整体异常: {e}")
            logger.error(f"process_pending_cleanup 异常: {e}")
        return result


@with_retry(exceptions=(TimeoutError, Exception))
async def _transcribe_local_asr(asr, file_path: str):
    """ASR 转写本地文件（带指数退避重试，3 次失败后抛出由上层 mark_failed 跳过）。"""
    return await asr.transcribe_local_file(file_path)


@with_retry(exceptions=(Exception,))
async def _vectorize_content(rag, content) -> int:
    """向量化写入向量库（带指数退避重试，3 次失败后抛出由上层 mark_failed 跳过）。"""
    return await asyncio.to_thread(rag.add_video_content, content)


async def ingest_local_audio_file(
    file_path: str,
    original_filename: str,
    ingest_task_id: Optional[int] = None,
) -> dict:
    """从本地音频/视频文件入库：ASR 转写 → 切片 → 向量化。

    复用现有 ASRService.transcribe_local_file 与 RAGService.add_video_content，
    将本地文件接入与 B站/抖音相同的 ASR→切片→向量 流程。

    断点续传：传入 ingest_task_id 时，每个阶段完成会更新 IngestTask.stage/status，
    失败标记 mark_failed，完成标记 mark_done。未传入时保持原有行为不变（向后兼容）。

    Args:
        file_path: 本地文件路径（由调用方负责创建与清理）
        original_filename: 用户上传时的原始文件名，仅用于展示标题
        ingest_task_id: 可选的 IngestTask.id，用于持久化入库阶段进度

    Returns:
        {"bvid": ..., "title": ..., "chunks": ..., "content_length": ...}
    """
    import os
    import uuid
    from app.services.asr import ASRService
    from app.services.rag import RAGService
    from app.models import VideoContent, ContentSource, VideoCache
    from app.database import get_db_context
    from app.services import ingest_task_store

    async def _update_stage(stage: str, status: Optional[str] = None, error: Optional[str] = None) -> None:
        """更新 IngestTask 阶段进度，失败时仅记录日志不抛出。"""
        if ingest_task_id is None:
            return
        try:
            async with get_db_context() as db:
                await ingest_task_store.update_stage(db, ingest_task_id, stage, status=status, error=error)
                await db.commit()
        except Exception as stage_err:
            logger.warning(f"[IngestTask#{ingest_task_id}] 更新阶段 {stage} 失败: {stage_err}")

    # 生成全局唯一 bvid（UUID hex，32 字符以适配 VideoCache.bvid String(32)）
    bvid = uuid.uuid4().hex
    title = os.path.splitext(original_filename)[0] or original_filename or "本地文件"

    # 标记任务为 running，开始 ASR 阶段
    await _update_stage("asr", status="running")

    # 1. ASR 转写本地文件（_transcribe_local_asr 内部已带指数退避重试，3 次失败抛出）
    try:
        asr = ASRService()
        text = await _transcribe_local_asr(asr, file_path)
        if not text or len(text.strip()) < 20:
            raise RuntimeError(f"ASR 转写失败或内容过少: {original_filename}")
    except Exception as e:
        await _update_stage("asr", status="failed", error=str(e))
        if ingest_task_id is not None:
            try:
                async with get_db_context() as db:
                    await ingest_task_store.mark_failed(db, ingest_task_id, str(e))
                    await db.commit()
            except Exception as mark_err:
                logger.warning(f"[IngestTask#{ingest_task_id}] mark_failed 失败: {mark_err}")
        raise

    # ASR 完成，进入 embedding 阶段
    await _update_stage("embedding", status="running")

    # 2. 构造 VideoContent 并写入向量库（切片 + 向量化由 RAGService 内部完成）
    content = VideoContent(
        bvid=bvid,
        title=title,
        content=text,
        source=ContentSource.ASR,
        platform="local",
        description=original_filename,
    )
    # 本地文件使用独立的向量集合，避免与 B站/抖音数据混淆
    rag = RAGService(collection_name="local_files")
    # 清理可能存在的旧向量（本地文件入库通常无旧向量，保险起见）
    try:
        await asyncio.to_thread(rag.delete_video, bvid)
    except Exception:
        pass
    try:
        chunks = await _vectorize_content(rag, content)
        if chunks <= 0:
            raise RuntimeError("未生成可写入的向量文档")
    except Exception as e:
        await _update_stage("embedding", status="failed", error=str(e))
        if ingest_task_id is not None:
            try:
                async with get_db_context() as db:
                    await ingest_task_store.mark_failed(db, ingest_task_id, str(e))
                    await db.commit()
            except Exception as mark_err:
                logger.warning(f"[IngestTask#{ingest_task_id}] mark_failed 失败: {mark_err}")
        raise

    # 3. 写入 VideoCache 以便后续查询与管理
    async with get_db_context() as db:
        cache = VideoCache(
            bvid=bvid,
            platform="local",
            title=title,
            content=text,
            content_source=ContentSource.ASR.value,
            description=original_filename,
            is_processed=True,
        )
        db.add(cache)
        try:
            await db.commit()
        except Exception as e:
            logger.warning(f"写入 VideoCache 失败 [{bvid}]: {e}")

    # 标记任务完成
    if ingest_task_id is not None:
        try:
            async with get_db_context() as db:
                await ingest_task_store.mark_done(db, ingest_task_id)
                await db.commit()
        except Exception as mark_err:
            logger.warning(f"[IngestTask#{ingest_task_id}] mark_done 失败: {mark_err}")

    logger.info(
        f"本地文件入库完成: {original_filename}, bvid={bvid}, chunks={chunks}, text_len={len(text)}"
    )
    return {
        "bvid": bvid,
        "title": title,
        "chunks": chunks,
        "content_length": len(text),
    }


# ===== 入库任务恢复 =====
# 注：B站/抖音的入库流程在 app/routers/knowledge.py 的 _ingest_single_video
# 中实现，需要 session cookies / folder_id 等运行时上下文，无法在 lifespan
# 启动时无副作用地恢复。当前仅 data_syncer.ingest_local_audio_file 接入了
# IngestTask 持久化，本地文件上传任务可断点续传。B站/抖音入库任务的接入
# 留待后续：需先将 session 持久化或引入任务参数快照机制后再扩展。


async def resume_ingest_task(task) -> None:
    """从 IngestTask 恢复一个入库任务（从当前 stage 重新执行）。

    根据 platform 分发：
    - local: 从 payload 读取 file_path/original_filename，重新执行 ingest_local_audio_file
    - bilibili/douyin: 缺少 session 上下文，标记为 failed 并记录原因

    Args:
        task: IngestTask ORM 对象（status 应为 pending）
    """
    import os
    from app.database import get_db_context
    from app.services import ingest_task_store

    payload = ingest_task_store.load_payload(task)

    try:
        if task.platform == "local":
            file_path = payload.get("file_path")
            original_filename = payload.get("original_filename") or "本地文件"
            if not file_path or not os.path.exists(file_path):
                # 临时文件可能已被清理，无法恢复
                raise RuntimeError(
                    f"恢复失败：本地文件不存在 {file_path}（上传临时文件可能已被清理）"
                )
            await ingest_local_audio_file(
                file_path, original_filename, ingest_task_id=task.id
            )
        else:
            # bilibili/douyin 入库需要 session 上下文，重启后无法恢复
            raise RuntimeError(
                f"平台 {task.platform} 入库任务需要 session 上下文，"
                f"无法自动恢复，请手动重新发起"
            )
    except Exception as e:
        logger.warning(f"[IngestTask#{task.id}] 恢复失败 [{task.platform}/{task.video_id}]: {e}")
        try:
            async with get_db_context() as db:
                await ingest_task_store.mark_failed(db, task.id, str(e))
                await db.commit()
        except Exception as mark_err:
            logger.warning(f"[IngestTask#{task.id}] mark_failed 失败: {mark_err}")