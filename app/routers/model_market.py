"""
ClipMind 模型市场路由

提供本地模型一键下载、自动配置、删除管理功能。

支持三类模型：
- LLM (Ollama)：调 Ollama /api/pull 拉取量化模型
- 向量模型 (bge/m3e)：用 huggingface_hub.snapshot_download 下载
- ASR (faster-whisper)：复用 ASR 服务的 HF 下载路径

设计要点：
- 全局 asyncio.Semaphore(1) 限制同一时刻只下载一个模型，避免磁盘/网络打满
- SSE 实时推送进度，前端无需轮询
- apply 接口负责"下载完成→自动写 settings.json + 热加载"
- 切换向量模型时强制校验维度，避免已入库数据检索失效
- 启动时清理中断的下载任务状态（进程崩溃恢复）
"""
import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from app.settings_store import load_settings, save_settings
from app.config import reload_settings

router = APIRouter(prefix="/api/model-market", tags=["模型市场"])


# ============================================================================
# 推荐模型目录（catalog）
# ============================================================================
# 每条记录字段说明：
#   id            模型市场内唯一 ID（前端通过它定位模型）
#   category      llm / embedding / asr
#   display_name  展示名
#   size_mb       预估大小（MB），仅用于 UI 提示，不强制校验
#   engine        ollama / hf_whisper / hf_embedding
#   model_id      实际拉取名（Ollama model 名 / HF repo_id / whisper size）
#   onnx_repo     （可选）hf_embedding 的 ONNX 权重仓库（transformers.js 格式，
#                 含 onnx/model_quantized.onnx）。配置后下载时自动补齐 ONNX 权重，
#                 使打包环境（无 torch）可用 onnxruntime 推理。
#   recommended   是否标记为推荐
#   description   描述文本

CATALOG: list[dict] = [
    # ====== LLM ======
    {
        "id": "qwen2.5:7b-instruct",
        "category": "llm",
        "display_name": "Qwen2.5 7B Instruct",
        "size_mb": 4700,
        "engine": "ollama",
        "model_id": "qwen2.5:7b-instruct",
        "recommended": True,
        "description": "通义千问 7B 量化版，中文友好，平衡显存与效果",
    },
    {
        "id": "qwen2.5:3b",
        "category": "llm",
        "display_name": "Qwen2.5 3B",
        "size_mb": 2000,
        "engine": "ollama",
        "model_id": "qwen2.5:3b",
        "recommended": False,
        "description": "更小尺寸，适合显存受限设备（4GB 即可运行）",
    },
    {
        "id": "llama3.1:8b",
        "category": "llm",
        "display_name": "Llama 3.1 8B",
        "size_mb": 4900,
        "engine": "ollama",
        "model_id": "llama3.1:8b",
        "recommended": False,
        "description": "Meta Llama 3.1 8B 量化版，英文与代码能力强",
    },
    # ====== 向量模型 ======
    {
        "id": "bge-small-zh-v1.5",
        "category": "embedding",
        "display_name": "BGE Small 中文",
        "size_mb": 95,
        "engine": "hf_embedding",
        "model_id": "BAAI/bge-small-zh-v1.5",
        "onnx_repo": "Xenova/bge-small-zh-v1.5",
        "recommended": True,
        "description": "智源 BGE 中文小模型，512 维，体积小、速度快",
    },
    {
        "id": "bge-large-zh-v1.5",
        "category": "embedding",
        "display_name": "BGE Large 中文",
        "size_mb": 1300,
        "engine": "hf_embedding",
        "model_id": "BAAI/bge-large-zh-v1.5",
        "onnx_repo": "Xenova/bge-large-zh-v1.5",
        "recommended": False,
        "description": "智源 BGE 中文大模型，1024 维，精度更高",
    },
    {
        "id": "m3e-base",
        "category": "embedding",
        "display_name": "M3E Base",
        "size_mb": 410,
        "engine": "hf_embedding",
        "model_id": "moka-ai/m3e-base",
        "recommended": False,
        "description": "M3E 中文嵌入模型，768 维，社区常用",
    },
    # ====== ASR ======
    {
        "id": "faster-whisper-small",
        "category": "asr",
        "display_name": "Whisper Small",
        "size_mb": 460,
        "engine": "hf_whisper",
        "model_id": "small",
        "recommended": False,
        "description": "faster-whisper small，速度快、显存占用低",
    },
    {
        "id": "faster-whisper-medium",
        "category": "asr",
        "display_name": "Whisper Medium",
        "size_mb": 1500,
        "engine": "hf_whisper",
        "model_id": "medium",
        "recommended": True,
        "description": "faster-whisper medium，平衡速度与准确率",
    },
    {
        "id": "faster-whisper-large-v3",
        "category": "asr",
        "display_name": "Whisper Large v3",
        "size_mb": 3000,
        "engine": "hf_whisper",
        "model_id": "large-v3",
        "recommended": False,
        "description": "faster-whisper large-v3，最高精度",
    },
]


# ============================================================================
# 下载任务管理（内存单例）
# ============================================================================

@dataclass
class DownloadTask:
    """单个下载任务的运行时状态。

    _stop_event 与 _subscribers 是运行时态，不进入 asdict 序列化。
    """
    task_id: str
    model_id: str           # catalog 中的 id（如 "qwen2.5:7b-instruct"）
    category: str           # llm / embedding / asr
    status: str = "pending" # pending / downloading / completed / failed / cancelled
    progress: float = 0.0   # 0.0 - 1.0
    downloaded_mb: float = 0.0
    total_mb: float = 0.0
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _subscribers: list = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        """供 SSE / status 接口返回的 dict 表示（剔除运行时态字段）。"""
        d = asdict(self)
        d.pop("_stop_event", None)
        d.pop("_subscribers", None)
        return d


# 全局任务表：task_id -> DownloadTask
_tasks: dict[str, DownloadTask] = {}
# 模型到任务的映射：model_id -> task_id（同一模型不重复下载）
_model_to_task: dict[str, str] = {}
# 全局并发限制：同一时刻只允许 1 个下载任务运行，避免磁盘/网络打满
_download_sem = asyncio.Semaphore(1)
# 进度事件总线：所有 SSE 客户端订阅此 asyncio.Queue
_event_bus: list[asyncio.Queue] = []


def _publish(event: dict) -> None:
    """向所有 SSE 订阅者广播事件（非阻塞，队列满则丢弃避免阻塞下载协程）。"""
    for q in list(_event_bus):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 队列满说明前端消费慢，丢弃此事件避免反压拖慢下载
            logger.debug(f"[ModelMarket] SSE 队列满，丢弃事件: {event.get('type')}")


def _resolve_catalog_entry(model_id: str) -> Optional[dict]:
    """根据 model_id 查找 catalog 条目。"""
    for item in CATALOG:
        if item["id"] == model_id:
            return item
    return None


# ============================================================================
# 已下载状态检测
# ============================================================================

async def _check_ollama_installed() -> tuple[bool, str]:
    """检测本地 Ollama 服务是否可用。

    返回 (installed, error_message)。
    """
    raw = load_settings()
    base_url = (raw.get("ollama_base_url") or "").strip() or "http://localhost:11434"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            if resp.status_code == 200:
                return True, ""
            return False, f"Ollama 返回 HTTP {resp.status_code}"
    except Exception as e:
        return False, f"无法连接 Ollama（{base_url}）：{e}"


async def _list_ollama_models() -> list[str]:
    """列出本地 Ollama 已下载的模型。"""
    raw = load_settings()
    base_url = (raw.get("ollama_base_url") or "").strip() or "http://localhost:11434"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [m.get("model", "") for m in (data.get("models") or []) if m.get("model")]
    except Exception as e:
        logger.warning(f"[ModelMarket] 列出 Ollama 模型失败: {e}")
        return []


def _local_embedding_path(model_id: str) -> str:
    """本地向量模型存储路径。

    存放位置：<data_dir>/models/embeddings/<model_id>
    """
    data_dir = os.environ.get("CLIPMIND_DATA_DIR", "data")
    return os.path.join(data_dir, "models", "embeddings", model_id.replace("/", "_"))


def _is_embedding_downloaded(model_id: str) -> bool:
    """检查本地向量模型是否已下载（完整）。

    判定条件：目录存在 + config.json 存在 + 至少一个权重文件存在。
    权重文件包括 pytorch 权重（model.safetensors 等）与 ONNX 权重
    （onnx/model_quantized.onnx 等），任一存在即视为完整。
    仅检查 config.json 会导致不完整下载（缺权重文件）被误判为已完成。
    """
    path = _local_embedding_path(model_id)
    if not os.path.isdir(path) or not os.path.exists(os.path.join(path, "config.json")):
        return False
    # 必须存在至少一个权重文件，否则下载不完整
    weight_files = (
        "model.safetensors", "pytorch_model.bin", "model.bin",
        *_ONNX_WEIGHT_FILES,
    )
    return any(os.path.exists(os.path.join(path, wf)) for wf in weight_files)


def _local_whisper_path(size: str) -> str:
    """本地 faster-whisper 模型存储路径。"""
    data_dir = os.environ.get("CLIPMIND_DATA_DIR", "data")
    return os.path.join(data_dir, "models", f"faster-whisper-{size}")


def _is_whisper_downloaded(size: str) -> bool:
    """检查本地 whisper 模型是否已下载。

    判定条件：目录存在且包含 model.bin（核心权重文件）。
    """
    path = _local_whisper_path(size)
    return os.path.isdir(path) and (
        os.path.exists(os.path.join(path, "model.bin"))
        or os.path.exists(os.path.join(path, "model.safetensors"))
    )


async def _build_status_map() -> dict[str, dict]:
    """构建所有模型的当前状态映射：model_id -> {downloaded, active, downloading}"""
    # 注意：await f()[0] 会被解析为 await (f()[0])，coroutine 不可 subscript，必须先 await 再取下标
    ollama_ok, _ = await _check_ollama_installed()
    ollama_models = await _list_ollama_models() if ollama_ok else []
    raw = load_settings()
    active_llm = raw.get("llm_provider") == "ollama" and raw.get("ollama_model") or ""
    active_embedding = raw.get("embedding_provider") == "local" and raw.get("embedding_model") or ""
    active_asr = raw.get("asr_provider", "local") == "local" and raw.get("asr_model_local") or ""

    status_map: dict[str, dict] = {}
    for item in CATALOG:
        mid = item["id"]
        engine = item["engine"]
        downloaded = False
        active = False
        onnx_missing = False
        if engine == "ollama":
            downloaded = mid in ollama_models
            active = (active_llm == mid)
        elif engine == "hf_embedding":
            downloaded = _is_embedding_downloaded(item["model_id"])
            # active 时 embedding_model 字段存的是本地路径
            # bool() 显式转换，避免 "" 短路求值返回空字符串而非 False
            active = bool(active_embedding) and active_embedding.endswith(mid.replace("/", "_"))
            # ONNX 权重缺失标记：已下载但缺 onnx（旧版本下载的模型），
            # 提示用户重新下载以补齐 ONNX 权重（打包环境无 torch，必须走 onnxruntime）
            if downloaded and item.get("onnx_repo"):
                local_emb_dir = _local_embedding_path(item["model_id"])
                onnx_missing = not any(
                    os.path.exists(os.path.join(local_emb_dir, wf))
                    for wf in _ONNX_WEIGHT_FILES
                )
        elif engine == "hf_whisper":
            downloaded = _is_whisper_downloaded(item["model_id"])
            active = (active_asr == item["model_id"])
        # 是否正在下载中
        downloading = mid in _model_to_task and _tasks.get(_model_to_task[mid]) and \
            _tasks[_model_to_task[mid]].status in ("pending", "downloading")
        status_map[mid] = {
            "downloaded": downloaded,
            "active": active,
            "downloading": bool(downloading),
            "onnx_missing": onnx_missing,
        }
    return status_map


# ============================================================================
# 下载执行器
# ============================================================================

async def _download_ollama(task: DownloadTask, model_id: str) -> None:
    """通过 Ollama /api/pull 流式拉取模型。

    Ollama 的 pull 接口返回 NDJSON，每行包含 status / completed / total 字段。
    """
    raw = load_settings()
    base_url = (raw.get("ollama_base_url") or "").strip() or "http://localhost:11434"
    url = f"{base_url.rstrip('/')}/api/pull"
    task.status = "downloading"
    _publish({"type": "started", "task_id": task.task_id, "model_id": model_id})

    try:
        timeout = httpx.Timeout(None, connect=10.0)  # 大模型下载不设总超时，仅设连接超时
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json={"name": model_id}) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(f"Ollama pull HTTP {resp.status_code}: {body[:200]}")
                async for line in resp.aiter_lines():
                    if task._stop_event.is_set():
                        raise asyncio.CancelledError("用户取消下载")
                    if not line:
                        continue
                    try:
                        import json
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    status = chunk.get("status", "")
                    if status == "pulling":
                        completed = chunk.get("completed", 0) or 0
                        total = chunk.get("total", 0) or 0
                        if total > 0:
                            task.downloaded_mb = completed / (1024 * 1024)
                            task.total_mb = total / (1024 * 1024)
                            task.progress = completed / total
                            _publish({
                                "type": "progress",
                                "task_id": task.task_id,
                                "model_id": model_id,
                                "progress": task.progress,
                                "downloaded_mb": round(task.downloaded_mb, 2),
                                "total_mb": round(task.total_mb, 2),
                            })
                    elif status == "success":
                        task.progress = 1.0
                        task.status = "completed"
                        task.completed_at = time.time()
                        _publish({"type": "completed", "task_id": task.task_id, "model_id": model_id})
                        return
    except asyncio.CancelledError:
        task.status = "cancelled"
        task.completed_at = time.time()
        _publish({"type": "cancelled", "task_id": task.task_id, "model_id": model_id})
        raise
    except Exception as e:
        task.status = "failed"
        task.error = str(e)[:300]
        task.completed_at = time.time()
        _publish({
            "type": "failed",
            "task_id": task.task_id,
            "model_id": model_id,
            "error": task.error,
        })
        logger.warning(f"[ModelMarket] Ollama 下载失败 {model_id}: {e}")


def _calc_dir_size_mb(directory: str) -> float:
    """计算目录下所有文件的总大小（MB），跳过临时文件。"""
    total_bytes = 0
    for root, _, files in os.walk(directory):
        for f in files:
            # 跳过 HuggingFace 下载临时文件
            if f.endswith((".lock", ".incomplete", ".tmp")):
                continue
            try:
                total_bytes += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total_bytes / (1024 * 1024)


def _validate_model_files(local_dir: str, weight_files: tuple[str, ...], model_id: str) -> None:
    """下载完成后验证权重文件存在，防止 LFS 文件未下载但 snapshot_download 静默返回。

    若权重文件缺失，清理半成品目录并抛出异常，让调用方标记任务为 failed。
    """
    for wf in weight_files:
        if os.path.exists(os.path.join(local_dir, wf)):
            return  # 找到至少一个权重文件，验证通过

    # 权重文件全部缺失：清理半成品目录，抛出异常
    import shutil
    if os.path.isdir(local_dir):
        shutil.rmtree(local_dir, ignore_errors=True)
    raise RuntimeError(
        f"模型 {model_id} 下载不完整：目录中缺少权重文件 {weight_files}。"
        f"可能原因：HuggingFace 镜像 LFS 文件下载失败。请重试下载。"
    )


# ONNX 权重文件候选（transformers.js 仓库惯例：onnx/model_quantized.onnx 优先）
_ONNX_WEIGHT_FILES = (
    "onnx/model_quantized.onnx",
    "onnx/model.onnx",
    "onnx/model_int8.onnx",
)


def _download_onnx_weight(onnx_repo: str, local_dir: str, hf_hub_download) -> str:
    """从 transformers.js 仓库下载 ONNX 权重到本地模型目录的 onnx/ 子目录。

    优先 model_quantized.onnx（int8 量化，体积小、速度快），
    缺失时回退 model.onnx（fp32）。
    返回下载后的本地路径。
    """
    import os
    for candidate in ("onnx/model_quantized.onnx", "onnx/model.onnx", "onnx/model_int8.onnx"):
        try:
            # local_dir 指定后 hf_hub_download 会把文件写入该目录（含 onnx/ 前缀）
            path = hf_hub_download(
                repo_id=onnx_repo,
                filename=candidate,
                local_dir=local_dir,
            )
            # 确保权重位于 <local_dir>/onnx/ 下（hf_hub_download 会保留 filename 相对路径）
            logger.info(f"[ModelMarket] ONNX 权重下载成功: {onnx_repo}/{candidate}")
            return path
        except Exception as e:
            logger.warning(f"[ModelMarket] ONNX 权重 {candidate} 下载失败: {e}")
    raise RuntimeError(
        f"ONNX 权重下载失败（{onnx_repo}），候选文件均不可用。"
        f"请检查网络或镜像配置后重试。"
    )


async def _download_hf(task: DownloadTask, repo_id: str, local_dir: str, is_whisper: bool) -> None:
    """通过 huggingface_hub.snapshot_download 下载模型。

    is_whisper=True 时下载 faster-whisper 模型（repo_id 固定为 Systran/faster-whisper-<size>）。
    hf_embedding 且 catalog 配置了 onnx_repo 时，下载完成后自动补齐 ONNX 权重，
    使打包环境（无 torch）可用 onnxruntime 推理。

    进度上报策略：
    - huggingface_hub.snapshot_download 不支持原生进度回调
    - 在线程池中执行同步下载，主协程轮询取消信号 + 目录大小变化
    - 基于 catalog 中预录的 size_mb 估算进度，封顶 0.95 防止假完成
    - 下载完成后精确计算实际大小并设为 1.0
    """
    task.status = "downloading"
    _publish({"type": "started", "task_id": task.task_id, "model_id": task.model_id})

    try:
        # 应用 HF 镜像（与 asr.py 一致，避免国内网络问题）
        from app.services.asr import _apply_hf_mirror
        mirror = _apply_hf_mirror()
        if mirror:
            logger.info(f"[ModelMarket] HF 下载使用镜像: {mirror}")

        # 获取预估总大小（从 catalog 中取 size_mb 用于进度估算）
        entry = _resolve_catalog_entry(task.model_id)
        expected_total_mb = float(entry.get("size_mb", 0)) if entry else 0.0
        if expected_total_mb > 0:
            task.total_mb = expected_total_mb
            _publish({
                "type": "progress",
                "task_id": task.task_id,
                "model_id": task.model_id,
                "progress": 0.0,
                "downloaded_mb": 0.0,
                "total_mb": round(expected_total_mb, 2),
            })

        from huggingface_hub import snapshot_download, hf_hub_download
        # 确保在 snapshot_download 调用前 Xet 已禁用
        #（_apply_hf_mirror 已 patch 一次，这里兜底防止 reload 时序问题）
        try:
            import huggingface_hub.constants as _hfc
            _hfc.HF_HUB_DISABLE_XET = True
        except Exception:
            pass

        # whisper 模型 repo 在 Systran 命名空间下
        actual_repo = f"Systran/faster-whisper-{repo_id}" if is_whisper else repo_id
        onnx_repo = (entry or {}).get("onnx_repo") or ""

        def _do_download():
            os.makedirs(os.path.dirname(local_dir), exist_ok=True)
            snapshot_download(
                repo_id=actual_repo,
                local_dir=local_dir,
                # 不指定 allow_patterns，下载全部文件
            )
            # 补齐 ONNX 权重（仅 hf_embedding + catalog 配置了 onnx_repo 时）
            if onnx_repo and not is_whisper:
                _download_onnx_weight(onnx_repo, local_dir, hf_hub_download)

        # 在线程池中执行同步下载，通过轮询 task._stop_event 实现取消
        download_future = asyncio.ensure_future(asyncio.to_thread(_do_download))

        # 轮询：取消信号 + 进度上报（每 2 秒扫描目录大小变化）
        last_report_time = 0.0
        while not download_future.done():
            if task._stop_event.is_set():
                download_future.cancel()
                raise asyncio.CancelledError("用户取消下载")
            await asyncio.sleep(1.0)

            # 每 2 秒上报一次目录大小变化
            now = time.time()
            if expected_total_mb > 0 and now - last_report_time >= 2.0:
                last_report_time = now
                current_mb = _calc_dir_size_mb(local_dir)
                task.downloaded_mb = round(current_mb, 2)
                # 估算进度，封顶 0.95（catalog size_mb 是预估值，防止假完成）
                estimated_progress = min(current_mb / expected_total_mb, 0.95)
                if estimated_progress > task.progress:
                    task.progress = round(estimated_progress, 4)
                    _publish({
                        "type": "progress",
                        "task_id": task.task_id,
                        "model_id": task.model_id,
                        "progress": task.progress,
                        "downloaded_mb": task.downloaded_mb,
                        "total_mb": task.total_mb,
                    })

        # 等待结果（抛出异常如有）
        await download_future

        # 下载完成后验证关键文件存在（防止 LFS 文件未下载但 snapshot_download 静默返回）
        if is_whisper:
            _validate_model_files(local_dir, ("model.bin", "model.safetensors"), task.model_id)
        else:
            # 向量模型：pytorch 权重或 onnx 权重任一存在即视为完整
            _weight_candidates = (
                "model.safetensors", "pytorch_model.bin", "model.bin",
                *_ONNX_WEIGHT_FILES,
            )
            _validate_model_files(local_dir, _weight_candidates, task.model_id)

        # 下载完成，精确计算实际大小
        actual_mb = _calc_dir_size_mb(local_dir)
        task.total_mb = actual_mb
        task.downloaded_mb = actual_mb
        task.progress = 1.0
        task.status = "completed"
        task.completed_at = time.time()
        _publish({"type": "completed", "task_id": task.task_id, "model_id": task.model_id})
    except asyncio.CancelledError:
        task.status = "cancelled"
        task.completed_at = time.time()
        # 清理半成品目录
        try:
            import shutil
            if os.path.isdir(local_dir):
                shutil.rmtree(local_dir, ignore_errors=True)
        except Exception:
            pass
        _publish({"type": "cancelled", "task_id": task.task_id, "model_id": task.model_id})
        raise
    except Exception as e:
        task.status = "failed"
        task.error = str(e)[:300]
        task.completed_at = time.time()
        _publish({
            "type": "failed",
            "task_id": task.task_id,
            "model_id": task.model_id,
            "error": task.error,
        })
        logger.warning(f"[ModelMarket] HF 下载失败 {repo_id}: {e}")


async def _run_download(task: DownloadTask) -> None:
    """下载任务入口：获取信号量后按 engine 分发。"""
    async with _download_sem:
        entry = _resolve_catalog_entry(task.model_id)
        if entry is None:
            task.status = "failed"
            task.error = f"未知的模型 ID: {task.model_id}"
            _publish({
                "type": "failed",
                "task_id": task.task_id,
                "model_id": task.model_id,
                "error": task.error,
            })
            return

        engine = entry["engine"]
        model_id = entry["model_id"]
        try:
            if engine == "ollama":
                await _download_ollama(task, model_id)
            elif engine == "hf_embedding":
                local_dir = _local_embedding_path(model_id)
                await _download_hf(task, model_id, local_dir, is_whisper=False)
            elif engine == "hf_whisper":
                local_dir = _local_whisper_path(model_id)
                await _download_hf(task, model_id, local_dir, is_whisper=True)
            else:
                raise RuntimeError(f"未知的 engine: {engine}")
        except asyncio.CancelledError:
            # 取消已在 _download_* 内处理状态
            pass
        finally:
            # 任务结束从映射表移除（保留在 _tasks 中供查询历史）
            _model_to_task.pop(task.model_id, None)


# ============================================================================
# 向量维度校验
# ============================================================================

# 已知本地向量模型的维度表（用于切换前校验已入库数据是否兼容）
_EMBEDDING_DIMS: dict[str, int] = {
    "bge-small-zh-v1.5": 512,
    "bge-large-zh-v1.5": 1024,
    "m3e-base": 768,
}


def _get_embedding_dim(model_id: str) -> Optional[int]:
    """返回本地向量模型的维度。未知模型返回 None（不强制校验）。"""
    return _EMBEDDING_DIMS.get(model_id)


async def _check_vector_dim_compatible(new_model_id: str) -> tuple[bool, str]:
    """检查切换到新向量模型是否会与已入库数据维度冲突。

    返回 (compatible, message)。
    - 无已入库数据：兼容
    - 已入库数据维度与新模型一致：兼容
    - 维度不一致：不兼容，前端应提示用户先重新入库
    """
    new_dim = _get_embedding_dim(new_model_id)
    if new_dim is None:
        # 未知维度（自定义模型），跳过校验
        return True, ""

    try:
        from app.config import settings as _s
        from langchain_chroma import Chroma
        # 尝试用当前 embeddings 查询现有集合的维度
        # 这里只读不写，用现有 RAGService 实例化方式获取集合
        from app.services.rag import RAGService
        rag = RAGService(collection_name="bilibili_videos")
        # 通过 chroma 内部 _collection 拿到现有向量维度
        coll = getattr(rag.vectorstore, "_collection", None)
        if coll is None:
            return True, ""
        count = coll.count()
        if count == 0:
            return True, ""
        # 已有数据，检查维度：取一条样本
        sample = coll.get(limit=1, include=["embeddings"])
        embs = sample.get("embeddings") if sample else None
        if not embs:
            return True, ""
        existing_dim = len(embs[0]) if embs else 0
        if existing_dim == new_dim:
            return True, ""
        return False, (
            f"已入库数据维度为 {existing_dim}，新模型 {new_model_id} 维度为 {new_dim}，"
            f"维度不一致会导致检索失效。请先在知识库页清空已入库内容后再切换。"
        )
    except Exception as e:
        # 校验失败不阻塞切换，但记录警告
        logger.warning(f"[ModelMarket] 向量维度校验异常（跳过）: {e}")
        return True, ""


# ============================================================================
# API 路由
# ============================================================================

class DownloadRequest(BaseModel):
    model_id: str  # catalog 中的 id


class ApplyRequest(BaseModel):
    model_id: str


class DeleteRequest(BaseModel):
    model_id: str


@router.get("/catalog")
async def get_catalog():
    """返回推荐模型清单 + 当前下载状态 + Ollama 安装状态。"""
    ollama_installed, ollama_err = await _check_ollama_installed()
    status_map = await _build_status_map()
    return {
        "models": [
            {**item, **status_map.get(item["id"], {})}
            for item in CATALOG
        ],
        "ollama_installed": ollama_installed,
        "ollama_error": ollama_err,
    }


@router.get("/status")
async def get_status():
    """查询所有任务的状态快照（不订阅 SSE 时的轮询兜底接口）。"""
    return {
        "tasks": [t.to_dict() for t in _tasks.values()],
        "models": await _build_status_map(),
    }


@router.post("/download")
async def download_model(payload: DownloadRequest):
    """触发模型下载。立即返回 task_id，进度通过 SSE 推送。"""
    entry = _resolve_catalog_entry(payload.model_id)
    if entry is None:
        return {"ok": False, "error": f"未知的模型 ID: {payload.model_id}"}

    # 同一模型已在下载中，直接返回现有 task_id
    existing_task_id = _model_to_task.get(payload.model_id)
    if existing_task_id and existing_task_id in _tasks:
        existing = _tasks[existing_task_id]
        if existing.status in ("pending", "downloading"):
            return {"ok": True, "task_id": existing_task_id, "message": "任务已存在"}

    # Ollama 模型需先检查 Ollama 是否安装
    if entry["engine"] == "ollama":
        installed, err = await _check_ollama_installed()
        if not installed:
            return {"ok": False, "error": f"Ollama 未安装或未运行：{err}"}

    task_id = str(uuid.uuid4())
    task = DownloadTask(
        task_id=task_id,
        model_id=payload.model_id,
        category=entry["category"],
    )
    _tasks[task_id] = task
    _model_to_task[payload.model_id] = task_id
    asyncio.create_task(_run_download(task))
    return {"ok": True, "task_id": task_id}


@router.post("/cancel")
async def cancel_download(payload: DownloadRequest):
    """取消正在进行的下载任务。"""
    task_id = _model_to_task.get(payload.model_id)
    if not task_id or task_id not in _tasks:
        return {"ok": False, "error": "无进行中的下载任务"}
    task = _tasks[task_id]
    if task.status not in ("pending", "downloading"):
        return {"ok": False, "error": f"任务状态为 {task.status}，无法取消"}
    task._stop_event.set()
    return {"ok": True, "task_id": task_id}


@router.post("/apply")
async def apply_model(payload: ApplyRequest):
    """将已下载的模型应用为当前配置（写 settings.json + 热加载）。

    切换向量模型时会强制校验维度，不兼容则拒绝切换。
    """
    entry = _resolve_catalog_entry(payload.model_id)
    if entry is None:
        return {"ok": False, "error": f"未知的模型 ID: {payload.model_id}"}

    engine = entry["engine"]
    model_id = entry["model_id"]

    # 校验是否已下载
    if engine == "ollama":
        installed, _ = await _check_ollama_installed()
        if not installed:
            return {"ok": False, "error": "Ollama 未运行，无法应用 LLM 模型"}
        ollama_models = await _list_ollama_models()
        if payload.model_id not in ollama_models:
            return {"ok": False, "error": "模型尚未下载完成，无法应用"}
    elif engine == "hf_embedding":
        if not _is_embedding_downloaded(model_id):
            return {"ok": False, "error": "向量模型尚未下载完成，无法应用"}
        # 切换向量模型强制校验维度
        compatible, msg = await _check_vector_dim_compatible(model_id)
        if not compatible:
            return {"ok": False, "error": msg, "code": "dim_mismatch"}
    elif engine == "hf_whisper":
        if not _is_whisper_downloaded(model_id):
            return {"ok": False, "error": "ASR 模型尚未下载完成，无法应用"}

    # 写配置
    if engine == "ollama":
        save_settings({
            "llm_provider": "ollama",
            "ollama_model": payload.model_id,
        })
    elif engine == "hf_embedding":
        local_path = _local_embedding_path(model_id)
        save_settings({
            "embedding_provider": "local",
            "embedding_model": local_path,
        })
    elif engine == "hf_whisper":
        save_settings({
            "asr_provider": "local",
            "asr_model_local": model_id,
        })

    reload_settings()
    logger.info(f"[ModelMarket] 已应用模型 {payload.model_id} (engine={engine})")
    return {"ok": True, "model_id": payload.model_id, "engine": engine}


@router.post("/delete")
async def delete_model(payload: DeleteRequest):
    """删除已下载的本地模型文件。

    - Ollama 模型：调 /api/delete
    - HF 模型：删除本地目录
    - 当前已启用的模型不允许删除
    """
    entry = _resolve_catalog_entry(payload.model_id)
    if entry is None:
        return {"ok": False, "error": f"未知的模型 ID: {payload.model_id}"}

    # 防止删除当前已启用的模型
    status_map = await _build_status_map()
    if status_map.get(payload.model_id, {}).get("active"):
        return {"ok": False, "error": "该模型当前已启用，请先切换到其他模型后再删除"}

    engine = entry["engine"]
    model_id = entry["model_id"]

    if engine == "ollama":
        raw = load_settings()
        base_url = (raw.get("ollama_base_url") or "").strip() or "http://localhost:11434"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.delete(
                    f"{base_url.rstrip('/')}/api/delete",
                    json={"name": payload.model_id},
                )
                if resp.status_code != 200:
                    return {"ok": False, "error": f"Ollama 删除失败 HTTP {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "error": f"删除失败：{e}"}
    elif engine == "hf_embedding":
        import shutil
        local_dir = _local_embedding_path(model_id)
        if os.path.isdir(local_dir):
            shutil.rmtree(local_dir, ignore_errors=True)
    elif engine == "hf_whisper":
        import shutil
        local_dir = _local_whisper_path(model_id)
        if os.path.isdir(local_dir):
            shutil.rmtree(local_dir, ignore_errors=True)

    logger.info(f"[ModelMarket] 已删除模型 {payload.model_id}")
    return {"ok": True, "model_id": payload.model_id}


@router.get("/events")
async def sse_events(request: Request):
    """SSE 端点：推送下载进度事件。

    事件类型：started / progress / completed / failed / cancelled
    客户端断开连接时自动从订阅列表移除。
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _event_bus.append(queue)

    async def event_stream():
        try:
            # 立即推送一次当前所有任务状态（让新连接的前端能恢复现场）
            yield f"data: {__import__('json').dumps({'type': 'snapshot', 'tasks': [t.to_dict() for t in _tasks.values()]})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {__import__('json').dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield f": heartbeat\n\n"
        finally:
            if queue in _event_bus:
                _event_bus.remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关闭 nginx 缓冲
        },
    )


# ============================================================================
# 启动时清理中断的下载任务
# ============================================================================

def cleanup_interrupted_tasks() -> None:
    """应用启动时调用：将所有 downloading/pending 状态的任务标记为 failed。

    进程崩溃或异常退出后，未完成的下载任务状态需要重置，
    避免前端误判任务仍在进行。
    """
    cleaned = 0
    for task in _tasks.values():
        if task.status in ("pending", "downloading"):
            task.status = "failed"
            task.error = "进程重启，任务中断"
            task.completed_at = time.time()
            cleaned += 1
    _model_to_task.clear()
    if cleaned:
        logger.info(f"[ModelMarket] 清理 {cleaned} 个中断的下载任务")
