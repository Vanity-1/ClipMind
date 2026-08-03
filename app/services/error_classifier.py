"""错误分类器 - 区分临时性错误和永久性错误

用于视频处理流程中判断错误类型，决定是否可以自动重试。
"""

from enum import Enum
from typing import Optional


class ErrorStage(str, Enum):
    DOWNLOAD = "download"
    ASR = "asr"
    EMBEDDING = "embedding"
    VECTOR = "vector"
    NETWORK = "network"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    PERMISSION = "permission"
    INVALID_VIDEO = "invalid_video"
    CONFIG = "config"


TRANSIENT_ERRORS = {
    ErrorStage.DOWNLOAD,
    ErrorStage.NETWORK,
    ErrorStage.TIMEOUT,
    ErrorStage.ASR,
    ErrorStage.EMBEDDING,
    ErrorStage.VECTOR,
}

PERMANENT_ERRORS = {
    ErrorStage.NOT_FOUND,
    ErrorStage.PERMISSION,
    ErrorStage.INVALID_VIDEO,
    ErrorStage.CONFIG,
}

TRANSIENT_ERROR_STAGES = {e.value for e in TRANSIENT_ERRORS}
PERMANENT_ERROR_STAGES = {e.value for e in PERMANENT_ERRORS}


def classify_error(exception: Exception) -> ErrorStage:
    """根据异常类型和消息判断错误阶段。

    分类策略（按优先级）：
    1. 异常类型包含 Timeout → TIMEOUT
    2. 异常类型包含 Connection/Network → NETWORK
    3. HTTP 404 / 403 → NOT_FOUND / PERMISSION
    4. HTTP 401 / 鉴权 → PERMISSION
    5. 消息包含 "失效" / "不存在" / "not found" → NOT_FOUND
    6. 消息包含 "下载" / "download" / "音频" → DOWNLOAD
    7. 消息包含 "转写" / "ASR" / "asr" → ASR
    8. 消息包含 "向量" / "embedding" / "vector" → EMBEDDING
    9. 默认 → DOWNLOAD（视为临时性错误，可重试）
    """
    exc_type = type(exception).__name__.lower()
    exc_msg = str(exception).lower()

    if "timeout" in exc_type or "timeout" in exc_msg:
        return ErrorStage.TIMEOUT

    if any(kw in exc_type for kw in ("connection", "network", "connect")):
        return ErrorStage.NETWORK

    if hasattr(exception, "status_code"):
        status_code = getattr(exception, "status_code", None)
        if status_code == 404:
            return ErrorStage.NOT_FOUND
        if status_code in (401, 403):
            return ErrorStage.PERMISSION

    msg = exc_msg
    if any(kw in msg for kw in ("失效", "不存在", "not found", "已删除", "已下架", "被下架")):
        return ErrorStage.NOT_FOUND
    if any(kw in msg for kw in ("无权限", "权限", "permission", "forbidden", "unauthorized")):
        return ErrorStage.PERMISSION
    if any(kw in msg for kw in ("无效视频", "invalid video", "已 private", "私有视频", "视频不可访问")):
        return ErrorStage.INVALID_VIDEO
    if any(kw in msg for kw in ("下载", "download", "音频", "audio")):
        return ErrorStage.DOWNLOAD
    if any(kw in msg for kw in ("转写", "asr", "语音识别")):
        return ErrorStage.ASR
    if any(kw in msg for kw in ("向量", "embedding", "vector", "写入验证")):
        return ErrorStage.EMBEDDING

    return ErrorStage.DOWNLOAD


def is_transient(stage: ErrorStage) -> bool:
    """判断错误阶段是否为临时性（可重试）。"""
    return stage in TRANSIENT_ERRORS


def is_permanent(stage: ErrorStage) -> bool:
    """判断错误阶段是否为永久性（不可重试）。"""
    return stage in PERMANENT_ERRORS