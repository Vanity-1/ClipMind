"""任务管理路由 - 统一的任务状态查询和操作接口"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from loguru import logger

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskListFilters(BaseModel):
    platform: Optional[str] = None
    status: Optional[str] = None
    video_id: Optional[str] = None
    session_id: Optional[str] = None


class BatchTaskRequest(BaseModel):
    task_ids: List[str]


@router.get("")
async def list_tasks(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    video_id: Optional[str] = None,
    session_id: Optional[str] = None,
):
    """查询任务列表（只读，无副作用）"""
    from app.services.task_tracker_service import task_tracker
    return await task_tracker.list_tasks(
        platform=platform,
        status=status,
        video_id=video_id,
        session_id=session_id,
    )


@router.get("/{task_id}")
async def get_task(task_id: str):
    """查询单个任务详情（只读）"""
    from app.services.task_tracker_service import task_tracker
    task = await task_tracker.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/{task_id}/error")
async def get_task_error(task_id: str):
    """查询任务错误详情（只读）"""
    from app.services.task_tracker_service import task_tracker
    task = await task_tracker.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": task_id,
        "error_stage": task.get("error_stage"),
        "error_message": task.get("error_message"),
        "retry_count": task.get("retry_count", 0),
        "permanent_failure": task.get("error_stage") in {"not_found", "permission", "invalid_video"},
    }


@router.post("/{task_id}/retry")
async def retry_task(task_id: str):
    """触发任务重试（写操作，返回新任务ID）"""
    from app.services.task_tracker_service import task_tracker
    new_task_id = await task_tracker.retry_task(task_id)
    if not new_task_id:
        raise HTTPException(status_code=400, detail="任务不可重试")
    return {"new_task_id": new_task_id, "status": "retried"}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消正在执行的任务"""
    from app.services.task_tracker_service import task_tracker
    success = await task_tracker.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="任务无法取消")
    return {"task_id": task_id, "status": "cancelled"}


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """删除任务记录"""
    from app.services.task_tracker_service import task_tracker
    success = await task_tracker.cleanup_expired_tasks(max_age_days=0, task_ids=[task_id])
    return {"task_id": task_id, "status": "deleted" if success else "not_found"}


@router.post("/batch/retry")
async def batch_retry(request: BatchTaskRequest):
    """批量重试任务"""
    from app.services.task_tracker_service import task_tracker
    results = []
    for task_id in request.task_ids:
        new_id = await task_tracker.retry_task(task_id)
        if new_id:
            results.append({"old_id": task_id, "new_id": new_id, "status": "retried"})
        else:
            results.append({"old_id": task_id, "status": "failed"})
    return {"results": results}


@router.post("/batch/cancel")
async def batch_cancel(request: BatchTaskRequest):
    """批量取消任务"""
    from app.services.task_tracker_service import task_tracker
    results = []
    for task_id in request.task_ids:
        success = await task_tracker.cancel_task(task_id)
        results.append({"task_id": task_id, "status": "cancelled" if success else "failed"})
    return {"results": results}


@router.delete("/batch/delete")
async def batch_delete(request: BatchTaskRequest):
    """批量删除任务"""
    from app.services.task_tracker_service import task_tracker
    success = await task_tracker.cleanup_expired_tasks(max_age_days=0, task_ids=request.task_ids)
    return {"task_ids": request.task_ids, "status": "deleted" if success else "partial"}