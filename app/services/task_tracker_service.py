"""
ClipMind -- 视频处理任务状态追踪服务

提供统一的任务生命周期管理，支持状态追踪、进度更新、重试、取消
与过期清理。采用单例模式，通过 asyncio.Lock 保证并发安全。

持久化策略：
- 所有任务状态同步写入 SQLite 的 task_records 表
- 进程重启时自动从 task_records 恢复到内存 _tasks 字典
- 内存字典用于快速读取，SQLite 用于持久化保障
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import NamedTuple, Optional

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, text

from app.services.error_classifier import (
    ErrorStage,
    classify_error,
    is_transient,
    TRANSIENT_ERROR_STAGES,
    PERMANENT_ERROR_STAGES,
)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class RetryInfo(NamedTuple):
    """auto_retry_failed_tasks 返回的单条重试信息。

    用于 main.py lifespan 中重新调度对应的任务函数。
    - old_task_id: 原失败任务 ID（已转为 RETRYING）
    - new_task_id: 新创建的 PENDING 任务 ID
    - task_type: 任务类型（build_knowledge_base / ingest_videos 等）
    - video_id: 新任务的 video_id（已加 :retry: 后缀，避免占用原始去重键）
    - metadata: 新任务的 metadata（含 session_id 等恢复参数）
    """

    old_task_id: str
    new_task_id: str
    task_type: Optional[str]
    video_id: str
    metadata: dict


_TERMINAL_STATUSES = {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}

# 合法的状态流转映射（仅约束显式通过 update_task 触发的转换；
# start_task / complete_task / mark_task_failed / cancel_task / retry_task
# 各自包含语义校验，不重复走此表）。
_ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {
        TaskStatus.RUNNING,
        TaskStatus.SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.RETRYING,
    },
    TaskStatus.RUNNING: {
        TaskStatus.SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.RETRYING,
    },
    TaskStatus.RETRYING: set(),  # 旧任务重试后的终态，不再变化
    TaskStatus.FAILED: {TaskStatus.RETRYING},
    TaskStatus.SUCCESS: set(),  # 终态
    TaskStatus.CANCELLED: set(),  # 终态
}


def _ts_to_dt(ts: Optional[float]) -> Optional[datetime]:
    """float 时间戳 → UTC datetime；None 则 None。"""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _dt_to_ts(dt: Optional[datetime]) -> Optional[float]:
    """UTC datetime → float 时间戳；None 则 None。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


class TaskInfo(BaseModel):
    task_id: str
    video_id: str
    status: TaskStatus = TaskStatus.PENDING
    current_step: str = ""
    progress: int = 0
    error_message: Optional[str] = None
    error_stage: Optional[str] = None
    retry_count: int = 0
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    platform: Optional[str] = None
    session_id: Optional[str] = None
    # 任务类型（build_knowledge_base / ingest_videos / batch_ingest_douyin 等），
    # 与 video_id 一起作为 create_task_if_not_exists 的去重键。
    task_type: Optional[str] = None
    # 业务元数据（total_videos / processed_videos / succeeded / failed / message 等），
    # 用于兼容旧版 build_tasks 字典结构，避免侵入核心字段。
    metadata: dict = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "video_id": self.video_id,
            "status": self.status.value,
            "current_step": self.current_step,
            "progress": self.progress,
            "error_message": self.error_message,
            "error_stage": self.error_stage,
            "retry_count": self.retry_count,
            "trace_id": self.trace_id,
            "platform": self.platform,
            "session_id": self.session_id,
            "task_type": self.task_type,
            "metadata": dict(self.metadata) if self.metadata else {},
            "created_at": datetime.fromtimestamp(self.created_at, tz=timezone.utc).isoformat(),
            "updated_at": datetime.fromtimestamp(self.updated_at, tz=timezone.utc).isoformat(),
            "started_at": (
                datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat()
                if self.started_at is not None
                else None
            ),
            "completed_at": (
                datetime.fromtimestamp(self.completed_at, tz=timezone.utc).isoformat()
                if self.completed_at is not None
                else None
            ),
        }


class TaskTracker:
    """视频处理任务状态追踪器（单例 + 持久化）。"""

    _instance: Optional[TaskTracker] = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __new__(cls) -> TaskTracker:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tasks: dict[str, TaskInfo] = {}
            cls._instance._write_lock = asyncio.Lock()
            cls._instance._loaded_from_db = False
            logger.info("TaskTracker 单例已初始化")
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例（仅用于测试）。"""
        cls._instance = None

    # ------------------------------------------------------------------
    # 持久化内部方法
    # ------------------------------------------------------------------

    async def _get_db(self):
        """获取异步数据库会话上下文。"""
        from app.database import async_session_factory
        return async_session_factory()

    async def _persist_task(self, task: TaskInfo) -> None:
        """将单个 TaskInfo 写入 task_records 表（UPSERT：INSERT or UPDATE）。

        异常仅记录日志，不抛出——持久化失败不应阻塞正常任务流程，
        最坏情况只是重启后该任务状态丢失（退化到旧版行为）。
        """
        try:
            from app.database import async_session_factory
            from app.models import TaskRecord

            async with async_session_factory() as db:
                stmt = select(TaskRecord).where(TaskRecord.task_id == task.task_id)
                result = await db.execute(stmt)
                rec = result.scalar_one_or_none()

                meta_json = dict(task.metadata) if task.metadata else None

                if rec is None:
                    rec = TaskRecord(
                        task_id=task.task_id,
                        video_id=task.video_id or "",
                        status=task.status.value,
                        current_step=task.current_step or "",
                        progress=task.progress,
                        error_message=task.error_message,
                        error_stage=task.error_stage,
                        retry_count=task.retry_count,
                        trace_id=task.trace_id,
                        platform=task.platform,
                        session_id=task.session_id,
                        task_type=task.task_type,
                        metadata_json=meta_json,
                        created_at=_ts_to_dt(task.created_at),
                        updated_at=_ts_to_dt(task.updated_at),
                        started_at=_ts_to_dt(task.started_at),
                        completed_at=_ts_to_dt(task.completed_at),
                    )
                    db.add(rec)
                else:
                    rec.video_id = task.video_id or ""
                    rec.status = task.status.value
                    rec.current_step = task.current_step or ""
                    rec.progress = task.progress
                    rec.error_message = task.error_message
                    rec.error_stage = task.error_stage
                    rec.retry_count = task.retry_count
                    rec.trace_id = task.trace_id
                    rec.platform = task.platform
                    rec.session_id = task.session_id
                    rec.task_type = task.task_type
                    rec.metadata_json = meta_json
                    rec.updated_at = _ts_to_dt(task.updated_at)
                    rec.started_at = _ts_to_dt(task.started_at)
                    rec.completed_at = _ts_to_dt(task.completed_at)
                await db.commit()
        except Exception as e:
            logger.error(f"TaskTracker 持久化失败 task_id={task.task_id}: {e}")

    async def _persist_delete(self, task_id: str) -> None:
        """从 task_records 删除单条任务。"""
        try:
            from app.database import async_session_factory
            from app.models import TaskRecord

            async with async_session_factory() as db:
                stmt = delete(TaskRecord).where(TaskRecord.task_id == task_id)
                await db.execute(stmt)
                await db.commit()
        except Exception as e:
            logger.error(f"TaskTracker DB 删除失败 task_id={task_id}: {e}")

    async def _ensure_loaded(self) -> None:
        """确保已从 task_records 表加载所有任务到内存。幂等。"""
        if self._loaded_from_db:
            return
        # 只有初始化后的第一次调用才加载；初始化阶段数据库可能还没 ready。
        try:
            from app.database import async_session_factory
            from app.models import TaskRecord

            async with async_session_factory() as db:
                stmt = select(TaskRecord)
                result = await db.execute(stmt)
                rows = result.scalars().all()

            loaded = 0
            for rec in rows:
                task = TaskInfo(
                    task_id=rec.task_id,
                    video_id=rec.video_id or "",
                    status=TaskStatus(rec.status) if rec.status else TaskStatus.PENDING,
                    current_step=rec.current_step or "",
                    progress=rec.progress or 0,
                    error_message=rec.error_message,
                    error_stage=rec.error_stage,
                    retry_count=rec.retry_count or 0,
                    trace_id=rec.trace_id or str(uuid.uuid4()),
                    platform=rec.platform,
                    session_id=rec.session_id,
                    task_type=rec.task_type,
                    metadata=dict(rec.metadata_json) if rec.metadata_json else {},
                    created_at=_dt_to_ts(rec.created_at) or time.time(),
                    updated_at=_dt_to_ts(rec.updated_at) or time.time(),
                    started_at=_dt_to_ts(rec.started_at),
                    completed_at=_dt_to_ts(rec.completed_at),
                )
                self._tasks[task.task_id] = task
                loaded += 1

            self._loaded_from_db = True
            logger.info(f"TaskTracker 从 DB 恢复了 {loaded} 个任务")
        except Exception as e:
            # DB 未就绪（比如 init_db 还没跑）时，退化到纯内存模式。
            logger.warning(f"TaskTracker 从 DB 加载失败，暂用纯内存模式: {e}")
            self._loaded_from_db = False

    # ------------------------------------------------------------------
    # 公开 API（全部保持兼容）
    # ------------------------------------------------------------------

    async def create_task(
        self,
        video_id: str,
        platform: Optional[str] = None,
        session_id: Optional[str] = None,
        task_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> TaskInfo:
        """创建新任务，返回任务对象，状态为 pending。"""
        async with self._write_lock:
            await self._ensure_loaded()
            task_id = str(uuid.uuid4())
            meta = dict(metadata or {})
            if video_id is not None:
                meta.setdefault("video_id", video_id)
            if task_type is not None:
                meta.setdefault("task_type", task_type)
            if platform is not None:
                meta.setdefault("platform", platform)
            if session_id is not None:
                meta.setdefault("session_id", session_id)
            task = TaskInfo(
                task_id=task_id,
                video_id=video_id,
                platform=platform,
                session_id=session_id,
                task_type=task_type,
                metadata=meta,
            )
            self._tasks[task_id] = task
            await self._persist_task(task)
            logger.info(
                "任务已创建: task_id={} video_id={} task_type={} trace_id={}",
                task_id, video_id, task_type or "-", task.trace_id,
            )
            return task

    async def create_task_if_not_exists(
        self,
        task_type: str,
        video_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """如果同一 video_id + task_type 的任务正在运行，返回 None；否则创建新任务。"""
        async with self._write_lock:
            await self._ensure_loaded()
            for tid, task in self._tasks.items():
                task_video = task.metadata.get("video_id") if task.metadata else None
                task_type_existing = task.metadata.get("task_type") if task.metadata else None
                # RETRYING 是终态（旧任务已被新任务替代），不应阻断新建。
                # 仅 PENDING/RUNNING 表示真正在执行中的任务。
                if (
                    task_video == video_id
                    and task_type_existing == task_type
                    and task.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
                ):
                    logger.info(
                        "任务去重命中: video_id={} task_type={} existing_task_id={} trace_id={}",
                        video_id, task_type, tid, task.trace_id,
                    )
                    return None

            task_id = str(uuid.uuid4())
            meta = dict(metadata or {})
            if video_id is not None:
                meta.setdefault("video_id", video_id)
            meta.setdefault("task_type", task_type)
            platform = meta.get("platform")
            session_id = meta.get("session_id")
            task = TaskInfo(
                task_id=task_id,
                video_id=video_id or "",
                status=TaskStatus.PENDING,
                platform=platform,
                session_id=session_id,
                task_type=task_type,
                metadata=meta,
            )
            self._tasks[task_id] = task
            await self._persist_task(task)
            logger.info(
                "任务已创建（去重）: task_id={} video_id={} task_type={} trace_id={}",
                task_id, video_id, task_type, task.trace_id,
            )
            return task_id

    async def start_task(self, task_id: str, step: str) -> None:
        """将任务状态改为 running，记录开始时间。"""
        async with self._write_lock:
            await self._ensure_loaded()
            task = self._get_task(task_id)
            task.status = TaskStatus.RUNNING
            task.current_step = step
            task.started_at = time.time()
            task.updated_at = time.time()
            await self._persist_task(task)
            logger.info(
                "任务已启动: task_id={} step={} trace_id={}",
                task_id, step, task.trace_id,
            )

    async def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        step: Optional[str] = None,
        progress: Optional[int] = None,
        error_message: Optional[str] = None,
        metadata: Optional[dict] = None,
        error_stage: Optional[str] = None,
    ) -> None:
        """更新任务状态、步骤、进度或错误信息。"""
        async with self._write_lock:
            await self._ensure_loaded()
            task = self._get_task(task_id)
            if status is not None and status != task.status:
                self._validate_transition(task.status, status)
                task.status = status
                # 当状态变为终态时，设置 completed_at（与 complete_task 保持一致）
                if status in _TERMINAL_STATUSES:
                    task.completed_at = time.time()
            if step is not None:
                task.current_step = step
            if progress is not None:
                task.progress = max(0, min(100, progress))
            if error_message is not None:
                task.error_message = error_message
            if error_stage is not None:
                task.error_stage = error_stage
            if metadata is not None:
                merged = dict(task.metadata)
                merged.update(metadata)
                merged["video_id"] = task.metadata.get("video_id", merged.get("video_id"))
                merged["task_type"] = task.metadata.get("task_type", merged.get("task_type"))
                task.metadata = merged
            task.updated_at = time.time()
            await self._persist_task(task)
            logger.debug(
                "任务已更新: task_id={} status={} progress={} trace_id={}",
                task_id, task.status.value, task.progress, task.trace_id,
            )

    async def complete_task(
        self, task_id: str, success: bool = True, error_message: Optional[str] = None
    ) -> None:
        """标记任务完成（success 或 failed）。"""
        async with self._write_lock:
            await self._ensure_loaded()
            task = self._get_task(task_id)
            task.status = TaskStatus.SUCCESS if success else TaskStatus.FAILED
            task.completed_at = time.time()
            task.updated_at = time.time()
            if error_message is not None:
                task.error_message = error_message
            await self._persist_task(task)
            level = logger.info if success else logger.error
            level(
                "任务已完成: task_id={} status={} trace_id={} error={}",
                task_id, task.status.value, task.trace_id, error_message or "none",
            )

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务，返回是否成功取消。"""
        async with self._write_lock:
            await self._ensure_loaded()
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning(
                    "任务不存在，无法取消: task_id={}",
                    task_id,
                )
                return False
            if task.status in _TERMINAL_STATUSES:
                logger.warning(
                    "任务已处于终态，无法取消: task_id={} status={} trace_id={}",
                    task_id, task.status.value, task.trace_id,
                )
                return False
            task.status = TaskStatus.CANCELLED
            task.completed_at = time.time()
            task.updated_at = time.time()
            await self._persist_task(task)
            logger.warning(
                "任务已取消: task_id={} trace_id={}",
                task_id, task.trace_id,
            )
            return True

    MAX_RETRY_COUNT = 3
    RETRY_DELAY_SECONDS = 30

    async def should_retry(self, task_id: str) -> bool:
        """判断任务是否应该重试。"""
        async with self._write_lock:
            await self._ensure_loaded()
            task = self._get_task(task_id)
            if task.status != TaskStatus.FAILED:
                return False
            if task.retry_count >= self.MAX_RETRY_COUNT:
                logger.warning(
                    "任务重试次数已达上限: task_id={} retry_count={} max={} trace_id={}",
                    task_id, task.retry_count, self.MAX_RETRY_COUNT, task.trace_id,
                )
                return False
            error_stage = getattr(task, "error_stage", None)
            if error_stage and error_stage in PERMANENT_ERROR_STAGES:
                logger.warning(
                    "任务为永久性错误，不可重试: task_id={} stage={} trace_id={}",
                    task_id, error_stage, task.trace_id,
                )
                return False
            return True

    async def auto_retry_failed_tasks(self) -> list[RetryInfo]:
        """自动重试符合条件的失败任务，返回重试信息列表。

        会先 _ensure_loaded 确保从 DB 恢复了所有任务。
        返回的 RetryInfo 列表供 main.py lifespan 重新调度对应的任务函数。
        """
        await self._ensure_loaded()
        retried: list[RetryInfo] = []
        async with self._write_lock:
            failed_tasks = [
                (tid, t) for tid, t in self._tasks.items()
                if t.status == TaskStatus.FAILED
            ]
        for task_id, task in failed_tasks:
            should = await self.should_retry(task_id)
            if should:
                new_id = await self.retry_task(task_id)
                if new_id:
                    new_task = self._tasks.get(new_id)
                    retried.append(RetryInfo(
                        old_task_id=task_id,
                        new_task_id=new_id,
                        task_type=task.task_type,
                        video_id=(new_task.video_id if new_task else task.video_id),
                        metadata=dict(new_task.metadata) if new_task and new_task.metadata else {},
                    ))
                    logger.info(
                        "自动重试任务: old_task_id={} new_task_id={} retry_count={} trace_id={}",
                        task_id, new_id, task.retry_count, task.trace_id,
                    )
        if retried:
            logger.info(f"批量自动重试完成: {len(retried)} 个任务已重试")
        return retried

    async def mark_task_failed(
        self,
        task_id: str,
        error_message: str,
        error_stage: Optional[str] = None,
    ) -> None:
        """标记任务失败并记录错误阶段信息。"""
        async with self._write_lock:
            await self._ensure_loaded()
            task = self._get_task(task_id)
            task.status = TaskStatus.FAILED
            task.error_message = error_message
            task.completed_at = time.time()
            task.updated_at = time.time()
            if error_stage:
                task.error_stage = error_stage
            await self._persist_task(task)
            logger.error(
                "任务失败: task_id={} stage={} retry_count={} trace_id={} error={}",
                task_id, error_stage or "unknown", task.retry_count, task.trace_id, error_message,
            )

    async def retry_task(self, task_id: str) -> Optional[str]:
        """重试任务，创建新任务并返回新 task_id；不可重试返回 None。"""
        async with self._write_lock:
            await self._ensure_loaded()
            old_task = self._tasks.get(task_id)
            if old_task is None:
                logger.warning("任务不存在，无法重试: task_id={}", task_id)
                return None
            if old_task.status != TaskStatus.FAILED:
                logger.warning(
                    "任务状态非 FAILED，不可重试: task_id={} status={}",
                    task_id, old_task.status.value,
                )
                return None
            if old_task.retry_count >= self.MAX_RETRY_COUNT:
                logger.warning(
                    "任务重试次数已达上限: task_id={} retry_count={} max={}",
                    task_id, old_task.retry_count, self.MAX_RETRY_COUNT,
                )
                return None
            error_stage = getattr(old_task, "error_stage", None)
            if error_stage and error_stage in PERMANENT_ERROR_STAGES:
                logger.warning(
                    "任务为永久性错误，不可重试: task_id={} stage={}",
                    task_id, error_stage,
                )
                return None

            new_task_id = str(uuid.uuid4())
            # 新任务 video_id 加随机后缀，避免占用原始去重键。
            # create_task_if_not_exists 的去重检查基于 metadata["video_id"]，
            # 因此需同步更新 metadata 中的 video_id，否则新任务仍会阻塞原始键。
            new_video_id = f"{old_task.video_id}:retry:{uuid.uuid4().hex[:8]}"
            new_meta = dict(old_task.metadata) if old_task.metadata else {}
            if "video_id" in new_meta:
                new_meta["video_id"] = new_video_id
            new_task = TaskInfo(
                task_id=new_task_id,
                video_id=new_video_id,
                status=TaskStatus.PENDING,
                platform=old_task.platform,
                session_id=old_task.session_id,
                retry_count=old_task.retry_count + 1,
                task_type=old_task.task_type,
                metadata=new_meta,
            )
            self._tasks[new_task_id] = new_task
            old_task.status = TaskStatus.RETRYING
            old_task.updated_at = time.time()
            await self._persist_task(new_task)
            await self._persist_task(old_task)
            logger.info(
                "任务重试: old_task_id={} new_task_id={} new_video_id={} retry_count={} trace_id={}",
                task_id, new_task_id, new_video_id, new_task.retry_count, new_task.trace_id,
            )
            return new_task_id

    async def get_task(self, task_id: str) -> Optional[dict]:
        """获取任务信息（返回字典）；任务不存在返回 None。

        读操作不持有 _write_lock：asyncio 单线程模型下 dict 读取是原子的，
        _ensure_loaded 幂等且内部有 _loaded_from_db 守卫。
        旧实现持锁读导致前端轮询被 fire-and-forget 进度更新的写锁阻塞，
        造成"入库成功但前端仍显示处理中"的症状。
        """
        if not self._loaded_from_db:
            async with self._write_lock:
                await self._ensure_loaded()
        task = self._tasks.get(task_id)
        if task is None:
            return None
        return task.to_dict()

    async def list_tasks(
        self,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        video_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> list[dict]:
        """列出任务（可按平台、状态、视频ID、会话ID过滤）。

        读操作不持有 _write_lock，理由同 get_task。
        """
        if not self._loaded_from_db:
            async with self._write_lock:
                await self._ensure_loaded()
        tasks = list(self._tasks.values())
        if platform is not None:
            tasks = [t for t in tasks if t.platform == platform]
        if status is not None:
            try:
                status_enum = TaskStatus(status)
                tasks = [t for t in tasks if t.status == status_enum]
            except ValueError:
                pass
        if video_id is not None:
            tasks = [t for t in tasks if t.video_id == video_id]
        if session_id is not None:
            tasks = [t for t in tasks if t.session_id == session_id]
        return [t.to_dict() for t in tasks]

    async def cleanup_zombie_tasks(self) -> int:
        """清理上次进程崩溃遗留的 RUNNING/PENDING 僵尸任务，以及堆积的 RETRYING 任务。

        进程异常退出时，正在执行的任务会停留在 RUNNING/PENDING 状态，
        重启后这些任务永远不会自然结束，导致去重键被永久占用、用户无法重新入库。

        RETRYING 是旧任务被 auto_retry 替代后的终态。这些任务不会自然消失，
        长期积累会占用内存和 DB 空间。重启时统一清理，避免无限堆积。

        本方法将 RUNNING/PENDING 标记为 FAILED（供 auto_retry 按需重试），
        将 RETRYING 直接删除（已无用途，新任务已通过 :retry: 后缀独立存在）。

        Returns:
            被清理的僵尸任务数量。
        """
        async with self._write_lock:
            await self._ensure_loaded()
            zombie_statuses = {TaskStatus.PENDING, TaskStatus.RUNNING}
            zombies = [
                (tid, t) for tid, t in self._tasks.items()
                if t.status in zombie_statuses
            ]
            # RETRYING 任务：旧任务已被新任务替代，直接删除避免堆积
            retrying_tasks = [
                (tid, t) for tid, t in self._tasks.items()
                if t.status == TaskStatus.RETRYING
            ]
            cleaned = 0
            for tid, task in zombies:
                task.status = TaskStatus.FAILED
                task.error_message = "进程异常退出"
                task.completed_at = time.time()
                task.updated_at = time.time()
                await self._persist_task(task)
                cleaned += 1
                logger.warning(
                    "清理僵尸任务: task_id={} status={} trace_id={}",
                    tid, task.status.value, task.trace_id,
                )
            for tid, task in retrying_tasks:
                self._tasks.pop(tid, None)
                await self._persist_delete(tid)
                cleaned += 1
                logger.info(
                    "清理过期 RETRYING 任务: task_id={} trace_id={}",
                    tid, task.trace_id,
                )
            if cleaned:
                logger.info(
                    f"僵尸任务清理完成: {len(zombies)} 个 RUNNING/PENDING 标记为 FAILED，"
                    f"{len(retrying_tasks)} 个 RETRYING 已删除"
                )
            return cleaned

    async def cleanup_expired_tasks(
        self,
        max_age_hours: float = 24,
        max_age_days: float = 0,
        task_ids: Optional[list[str]] = None,
    ) -> bool:
        """清理过期或指定任务，返回是否至少清理了一个任务。

        同时从内存 _tasks 和 DB task_records 中删除。
        """
        async with self._write_lock:
            await self._ensure_loaded()
            if task_ids:
                for tid in task_ids:
                    self._tasks.pop(tid, None)
                    await self._persist_delete(tid)
                logger.info(f"按 ID 清理任务: {len(task_ids)} 个")
                return True

            now = time.time()
            max_age_seconds = max_age_hours * 3600 + max_age_days * 86400
            expired_ids: list[str] = []
            for task_id, task in self._tasks.items():
                if task.status not in _TERMINAL_STATUSES:
                    continue
                reference_time = task.completed_at or task.updated_at or task.created_at
                if now - reference_time > max_age_seconds:
                    expired_ids.append(task_id)
            for task_id in expired_ids:
                task = self._tasks.pop(task_id, None)
                if task:
                    await self._persist_delete(task_id)
                    logger.info(
                        "清理过期任务: task_id={} status={} trace_id={}",
                        task_id, task.status.value, task.trace_id,
                    )
            return len(expired_ids) > 0

    def _get_task(self, task_id: str) -> TaskInfo:
        """获取任务对象（内部方法，不加锁，不触发 _ensure_loaded）。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        return task

    def _validate_transition(self, current: TaskStatus, new: TaskStatus) -> None:
        """校验状态流转是否合法（内部方法，不加锁）。"""
        if current == new:
            return
        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if new not in allowed:
            raise ValueError(
                f"非法状态流转: {current.value} -> {new.value}（task_type 校验失败）"
            )


task_tracker = TaskTracker()
