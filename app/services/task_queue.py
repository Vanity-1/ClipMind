"""任务队列服务 - 支持并发控制和任务调度"""

import asyncio
from typing import Callable, Optional
from loguru import logger
from dataclasses import dataclass, field
from enum import Enum


class TaskPriority(int, Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


@dataclass
class QueueTask:
    task_id: str
    coro: Callable
    priority: TaskPriority = TaskPriority.NORMAL
    created_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())

    def __lt__(self, other):
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.created_at < other.created_at


class TaskQueue:
    """基于 asyncio 的任务队列，支持并发控制"""

    def __init__(self, max_concurrent: int = 5):
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.running = False
        self._workers: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()
        self._active_count = 0
        self._lock = asyncio.Lock()

    async def start(self):
        """启动任务队列"""
        if self.running:
            return
        self.running = True
        self._shutdown_event.clear()
        worker_count = min(4, self.max_concurrent)
        for i in range(worker_count):
            worker = asyncio.create_task(self._worker_loop(i))
            self._workers.append(worker)
        logger.info(f"任务队列已启动，最大并发={self.max_concurrent}, worker数={worker_count}")

    async def submit(self, task_id: str, coro: Callable, priority: TaskPriority = TaskPriority.NORMAL) -> None:
        """提交任务到队列"""
        if not self.running:
            raise RuntimeError("任务队列未启动")
        task = QueueTask(task_id=task_id, coro=coro, priority=priority)
        await self.queue.put(task)
        logger.debug(f"任务已提交: {task_id} priority={priority.name}")

    async def _worker_loop(self, worker_id: int):
        """Worker 循环：从队列取任务执行"""
        logger.debug(f"Worker-{worker_id} 已启动")
        while not self._shutdown_event.is_set():
            try:
                task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            async with self.semaphore:
                async with self._lock:
                    self._active_count += 1
                try:
                    logger.debug(f"Worker-{worker_id} 开始执行任务: {task.task_id}")
                    await task.coro()
                    logger.debug(f"Worker-{worker_id} 完成任务: {task.task_id}")
                except Exception as e:
                    logger.error(f"Worker-{worker_id} 任务异常 [{task.task_id}]: {e}")
                finally:
                    async with self._lock:
                        self._active_count -= 1
                    self.queue.task_done()

    async def shutdown(self, wait: bool = True):
        """优雅关闭"""
        logger.info("任务队列关闭中...")
        self._shutdown_event.set()
        self.running = False

        if wait:
            await self.queue.join()
            logger.info("队列中任务已全部完成")

        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("任务队列已关闭")

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def pending_count(self) -> int:
        return self.queue.qsize()

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "active": self._active_count,
            "pending": self.queue.qsize(),
            "max_concurrent": self.max_concurrent,
            "workers": len(self._workers),
        }