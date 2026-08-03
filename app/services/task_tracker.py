"""
ClipMind -- 构建任务状态跟踪器

集中管理跨平台批量入库任务的进度状态字典与 Pydantic 模型，
供 knowledge / douyin 等路由共享复用，避免循环导入。

注意：自统一任务状态管理后，入库流程已改为直接使用
``app.services.task_tracker_service.task_tracker`` 单例。
本模块保留 ``build_tasks`` 字典、``BuildStatus`` 模型与若干转换函数
作为前端轮询兼容层；新代码应直接使用 task_tracker_service。
"""
import time
from typing import Optional

from loguru import logger
from pydantic import BaseModel


# 构建任务状态（兼容层字典，入库流程不再写入；保留以兼容旧导入）
build_tasks: dict[str, dict] = {}
_BUILD_TASK_TTL_SEC = 24 * 3600  # 已完成任务保留 24 小时后自动清理


class BuildStatus(BaseModel):
    """构建状态"""
    task_id: str
    status: str  # pending / running / completed / failed
    progress: int  # 0-100
    current_step: str
    total_videos: int
    processed_videos: int
    total_folders: Optional[int] = None
    processed_folders: Optional[int] = None
    current_folder_id: Optional[int] = None
    current_folder_title: Optional[str] = None
    current_video_title: Optional[str] = None
    message: str
    succeeded: Optional[int] = None
    failed: Optional[int] = None


# TaskStatus -> build_tasks status 字符串映射
_TASK_STATUS_TO_BUILD_STATUS = {
    "pending": "pending",
    "running": "running",
    "success": "completed",
    "failed": "failed",
    "cancelled": "failed",
    "retrying": "running",
}


def prune_expired_build_tasks() -> None:
    """清理超过 TTL 的已完成 / 已失败任务，避免内存无限增长。

    注意：统一任务状态管理后，入库流程不再写入 build_tasks 字典，
    本函数退化为兼容 no-op。真正的过期清理由
    ``task_tracker.cleanup_expired_tasks`` 异步负责（见 tasks.py 路由）。
    保留函数签名以兼容 knowledge.py 旧调用点。
    """
    if not build_tasks:
        return
    now = time.time()
    expired = []
    for task_id, task in build_tasks.items():
        status = task.get("status")
        if status in ("completed", "failed"):
            finished_at = task.get("finished_at") or task.get("updated_at")
            if finished_at and now - finished_at > _BUILD_TASK_TTL_SEC:
                expired.append(task_id)
    for task_id in expired:
        build_tasks.pop(task_id, None)
        logger.info(f"清理过期构建任务（兼容层）: {task_id}")


def make_initial_task(task_id: str, total_videos: int, session_id: str) -> dict:
    """创建一个初始任务状态字典（用于后台批量入库）。

    注意：新代码应直接用 ``task_tracker.create_task_if_not_exists`` 创建
    TaskInfo；本函数仅保留以兼容仍有 build_tasks 写入的旧路径。
    """
    return {
        "status": "pending",
        "progress": 0,
        "current_step": "初始化中...",
        "total_videos": total_videos,
        "processed_videos": 0,
        "total_folders": None,
        "processed_folders": None,
        "current_folder_id": None,
        "current_folder_title": None,
        "current_video_title": None,
        "message": "",
        "succeeded": 0,
        "failed": 0,
        "session_id": session_id,
    }


def to_build_status(task: dict, task_id: str) -> BuildStatus:
    """将内部任务字典转为对外 BuildStatus 模型（兼容旧 build_tasks 字典）。"""
    return BuildStatus(
        task_id=task_id,
        status=task["status"],
        progress=task["progress"],
        current_step=task["current_step"],
        total_videos=task["total_videos"],
        processed_videos=task["processed_videos"],
        total_folders=task.get("total_folders"),
        processed_folders=task.get("processed_folders"),
        current_folder_id=task.get("current_folder_id"),
        current_folder_title=task.get("current_folder_title"),
        current_video_title=task.get("current_video_title"),
        message=task["message"],
        succeeded=task.get("succeeded"),
        failed=task.get("failed"),
    )


def task_info_to_build_status(task_id: str, task: dict) -> BuildStatus:
    """将 task_tracker.get_task 返回的字典转换为 BuildStatus 模型。

    用于替代旧的 ``to_build_status``，从 TaskInfo.to_dict() 输出中读取
    业务字段（存放在 metadata）并映射状态枚举。

    Args:
        task_id: 任务 ID。
        task: ``task_tracker.get_task(task_id)`` 返回的字典，包含
            status / progress / current_step / error_message / metadata 等字段。

    Returns:
        BuildStatus 模型，字段缺失时回退到默认值（0 / None / 空串）。
    """
    metadata = task.get("metadata") or {}
    status_raw = task.get("status", "pending")
    status = _TASK_STATUS_TO_BUILD_STATUS.get(status_raw, "pending")
    # message 优先取 metadata.message；若任务失败且有 error_message，则回退到错误信息
    message = metadata.get("message", "")
    if not message and status == "failed":
        message = task.get("error_message") or ""
    return BuildStatus(
        task_id=task_id,
        status=status,
        progress=task.get("progress", 0),
        current_step=task.get("current_step", ""),
        total_videos=metadata.get("total_videos", 0),
        processed_videos=metadata.get("processed_videos", 0),
        total_folders=metadata.get("total_folders"),
        processed_folders=metadata.get("processed_folders"),
        current_folder_id=metadata.get("current_folder_id"),
        current_folder_title=metadata.get("current_folder_title"),
        current_video_title=metadata.get("current_video_title"),
        message=message,
        succeeded=metadata.get("succeeded"),
        failed=metadata.get("failed"),
    )
