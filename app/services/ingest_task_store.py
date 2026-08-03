"""IngestTask 持久化读写。

为入库流程提供断点续传所需的原子读写原语：
- create_task：入库前创建一条 pending 任务，记录 payload（url/title 等）
- update_stage：每个阶段（download/transcode/asr/embedding/done）完成时更新进度
- mark_failed / mark_done：失败或完成时收尾
- get_pending_tasks：lifespan 启动时查询所有未完成任务（pending+running）用于恢复

所有函数均接受外部传入的 AsyncSession，由调用方管理事务边界与 commit。
"""
import json
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IngestTask


async def create_task(
    db: AsyncSession,
    video_id: str,
    platform: str,
    payload: dict,
    stage: str = "download",
) -> IngestTask:
    """创建一条 pending 状态的入库任务。

    Args:
        db: 异步会话（调用方负责 commit）
        video_id: 视频 ID（bvid / aweme_id / 本地文件生成的 uuid hex）
        platform: bilibili / douyin / local
        payload: 任务参数，会序列化为 payload_json 存储
        stage: 初始阶段，默认 download

    Returns:
        已写入会话（未 commit）的 IngestTask ORM 对象
    """
    task = IngestTask(
        video_id=video_id,
        platform=platform,
        stage=stage,
        status="pending",
        payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
    )
    db.add(task)
    await db.flush()  # 拿到自增 id，但不 commit，事务由调用方控制
    return task


async def update_stage(
    db: AsyncSession,
    task_id: int,
    stage: str,
    status: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """更新任务阶段与状态。

    Args:
        db: 异步会话（调用方负责 commit）
        task_id: IngestTask.id
        stage: 新阶段（download/transcode/asr/embedding/done）
        status: 可选新状态（pending/running/done/failed），不传则只更新 stage
        error: 失败时的错误信息，传入 None 不覆盖已有值
    """
    values: dict = {"stage": stage}
    if status is not None:
        values["status"] = status
    if error is not None:
        values["error"] = error
    await db.execute(
        update(IngestTask).where(IngestTask.id == task_id).values(**values)
    )


async def get_pending_tasks(db: AsyncSession) -> list[IngestTask]:
    """查询所有未完成的任务（status in pending/running）。

    用于 lifespan startup 恢复：返回的任务会按 video_id 升序，便于稳定排序与排查。
    """
    stmt = (
        select(IngestTask)
        .where(IngestTask.status.in_(("pending", "running")))
        .order_by(IngestTask.id.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def mark_failed(db: AsyncSession, task_id: int, error: str) -> None:
    """标记任务失败并记录错误信息。"""
    await db.execute(
        update(IngestTask)
        .where(IngestTask.id == task_id)
        .values(status="failed", error=error)
    )


async def mark_done(db: AsyncSession, task_id: int) -> None:
    """标记任务完成（stage=done, status=done）。"""
    await db.execute(
        update(IngestTask)
        .where(IngestTask.id == task_id)
        .values(status="done", stage="done", error=None)
    )


async def reset_running_to_pending(db: AsyncSession) -> int:
    """将所有 running 状态的任务重置为 pending。

    程序崩溃时正在执行的任务状态仍是 running，重启后无法判断真实进度，
    统一重置为 pending 后由恢复逻辑重新调度。返回受影响行数。
    """
    result = await db.execute(
        update(IngestTask)
        .where(IngestTask.status == "running")
        .values(status="pending")
    )
    return result.rowcount or 0


def load_payload(task: IngestTask) -> dict:
    """反序列化 payload_json，损坏或缺失时返回空 dict。"""
    if not task.payload_json:
        return {}
    try:
        data = json.loads(task.payload_json)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
