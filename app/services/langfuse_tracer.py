"""
Langfuse 全链路追踪服务

可选集成，默认关闭。配置环境变量后启用：
  LANGFUSE_ENABLED=true
  LANGFUSE_PUBLIC_KEY=pk-xxx
  LANGFUSE_SECRET_KEY=sk-xxx
  LANGFUSE_HOST=https://cloud.langfuse.com

与现有 tracing.py 的 trace_id 机制集成，
通过 ContextVar 传递 trace_id，零侵入式接入。
"""
from __future__ import annotations

import asyncio
import traceback
from contextvars import ContextVar
from typing import Any, Optional
from loguru import logger

# 尝试导入 langfuse，未安装时全部为 no-op
try:
    from langfuse import Langfuse  # type: ignore
    _LANGFUSE_AVAILABLE = True
except ImportError:
    _LANGFUSE_AVAILABLE = False


# 当前活跃的 span 栈（用于嵌套 span）
_span_stack_var: ContextVar[list[Any]] = ContextVar("langfuse_span_stack", default=[])
_current_trace_var: ContextVar[Any] = ContextVar("langfuse_current_trace", default=None)
_enabled_var: ContextVar[bool] = ContextVar("langfuse_enabled", default=False)

_client: Optional["Langfuse"] = None
_initialized = False


def is_enabled() -> bool:
    """返回 Langfuse 追踪是否已启用。"""
    return _initialized and _enabled_var.get() and _LANGFUSE_AVAILABLE


def initialize(
    public_key: str = "",
    secret_key: str = "",
    host: str = "https://cloud.langfuse.com",
    enabled: bool = False,
) -> bool:
    """初始化 Langfuse 客户端。

    返回 True 表示初始化成功并已启用，False 表示未启用或初始化失败。
    """
    global _client, _initialized

    if not enabled:
        logger.info("[Langfuse] 未启用（LANGFUSE_ENABLED=false）")
        _initialized = True
        _enabled_var.set(False)
        return False

    if not _LANGFUSE_AVAILABLE:
        logger.warning("[Langfuse] langfuse Python 包未安装，追踪功能不可用。请执行 pip install langfuse")
        _initialized = True
        _enabled_var.set(False)
        return False

    if not public_key or not secret_key:
        logger.warning("[Langfuse] 缺少 LANGFUSE_PUBLIC_KEY 或 LANGFUSE_SECRET_KEY，追踪功能不可用")
        _initialized = True
        _enabled_var.set(False)
        return False

    try:
        _client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        _initialized = True
        _enabled_var.set(True)
        logger.info(f"[Langfuse] 已启用（host={host}）")
        return True
    except Exception as e:
        logger.warning(f"[Langfuse] 初始化失败: {e}")
        _initialized = True
        _enabled_var.set(False)
        return False


def shutdown() -> None:
    """关闭 Langfuse 客户端，刷新所有待发送数据。"""
    global _client, _initialized
    if _client and _LANGFUSE_AVAILABLE:
        try:
            _client.flush()
        except Exception:
            pass
    _client = None
    _initialized = False
    _enabled_var.set(False)


def start_trace(name: str, trace_id: Optional[str] = None, metadata: Optional[dict] = None,
                tags: Optional[dict] = None) -> Optional[Any]:
    """开始一个新的 trace。

    与现有 trace_id 集成：如果 tracing.py 已设置 trace_id，直接复用。
    返回 trace 对象（未启用时返回 None）。
    """
    if not is_enabled() or _client is None:
        return None

    try:
        # 从 tracing.py 复用 trace_id
        from app.services.tracing import get_trace_id
        existing_id = trace_id or get_trace_id()
        if not existing_id:
            existing_id = name

        trace = _client.trace(
            id=existing_id,
            name=name,
            metadata=metadata or {},
            tags=tags or {},
        )
        _current_trace_var.set(trace)
        _span_stack_var.set([])
        logger.debug(f"[Langfuse] Trace started: {name} (id={existing_id})")
        return trace
    except Exception as e:
        logger.debug(f"[Langfuse] start_trace failed: {e}")
        return None


def end_trace(status: str = "success", output: Optional[dict] = None) -> None:
    """结束当前 trace。"""
    if not is_enabled() or _client is None:
        return

    try:
        trace = _current_trace_var.get()
        if trace is not None:
            trace.update(
                output=output or {},
                level="ERROR" if status == "error" else "DEFAULT",
            )
            logger.debug(f"[Langfuse] Trace ended: status={status}")
        _current_trace_var.set(None)
        _span_stack_var.set([])
    except Exception as e:
        logger.debug(f"[Langfuse] end_trace failed: {e}")


def start_span(name: str, input_data: Optional[dict] = None,
               metadata: Optional[dict] = None) -> Optional[Any]:
    """开始一个新的 span（嵌套在当前 span 或 trace 下）。"""
    if not is_enabled() or _client is None:
        return None

    try:
        trace = _current_trace_var.get()
        if trace is None:
            return None

        stack = _span_stack_var.get()
        parent = stack[-1] if stack else trace

        span = parent.span(
            name=name,
            input=input_data or {},
            metadata=metadata or {},
        )
        stack.append(span)
        _span_stack_var.set(stack)
        logger.debug(f"[Langfuse] Span started: {name}")
        return span
    except Exception as e:
        logger.debug(f"[Langfuse] start_span failed: {e}")
        return None


def end_span(status: str = "success", output: Optional[dict] = None,
             error: Optional[Exception] = None) -> None:
    """结束当前最内层的 span。"""
    if not is_enabled() or _client is None:
        return

    try:
        stack = _span_stack_var.get()
        if not stack:
            return

        span = stack.pop()
        _span_stack_var.set(stack)

        if error is not None:
            status = "error"
            output = output or {}
            output["error"] = str(error)
            output["traceback"] = traceback.format_exc()

        span.end(
            output=output or {},
            level="ERROR" if status == "error" else "DEFAULT",
        )
        logger.debug(f"[Langfuse] Span ended: {span.name or 'unknown'} (status={status})")
    except Exception as e:
        logger.debug(f"[Langfuse] end_span failed: {e}")


def set_tag(key: str, value: Any) -> None:
    """为当前 trace 设置标签。"""
    if not is_enabled() or _client is None:
        return

    try:
        trace = _current_trace_var.get()
        if trace is not None:
            current_tags = getattr(trace, "tags", {}) or {}
            current_tags[key] = value
            trace.update(tags=current_tags)
    except Exception as e:
        logger.debug(f"[Langfuse] set_tag failed: {e}")


# ---------- Context Manager helpers ----------

class TraceContext:
    """Trace 上下文管理器，配合 with 语句使用。

    用法：
        with TraceContext("douyin_qr_login", tags={"platform": "douyin"}):
            ... do work ...
    """

    def __init__(self, name: str, trace_id: Optional[str] = None,
                 metadata: Optional[dict] = None, tags: Optional[dict] = None):
        self.name = name
        self.trace_id = trace_id
        self.metadata = metadata
        self.tags = tags
        self.trace = None

    def __enter__(self):
        self.trace = start_trace(self.name, self.trace_id, self.metadata, self.tags)
        return self.trace

    def __exit__(self, exc_type, exc_val, exc_tb):
        status = "error" if exc_type is not None else "success"
        output = None
        if exc_val is not None:
            output = {"error": str(exc_val)}
        end_trace(status, output)
        return False  # 不吞异常


class SpanContext:
    """Span 上下文管理器，配合 with 语句使用。

    用法：
        with SpanContext("browser_launch", input={"headless": True}):
            ... do work ...
    """

    def __init__(self, name: str, input_data: Optional[dict] = None,
                 metadata: Optional[dict] = None):
        self.name = name
        self.input_data = input_data
        self.metadata = metadata
        self.span = None

    def __enter__(self):
        self.span = start_span(self.name, self.input_data, self.metadata)
        return self.span

    def __exit__(self, exc_type, exc_val, exc_tb):
        status = "error" if exc_type is not None else "success"
        output = None
        error = None
        if exc_val is not None:
            error = exc_val
        end_span(status, output, error)
        return False  # 不吞异常


# ---------- Async helpers ----------

class AsyncTraceContext:
    """异步 Trace 上下文管理器。"""

    def __init__(self, name: str, trace_id: Optional[str] = None,
                 metadata: Optional[dict] = None, tags: Optional[dict] = None):
        self.name = name
        self.trace_id = trace_id
        self.metadata = metadata
        self.tags = tags
        self.trace = None

    async def __aenter__(self):
        self.trace = start_trace(self.name, self.trace_id, self.metadata, self.tags)
        return self.trace

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        status = "error" if exc_type is not None else "success"
        output = None
        if exc_val is not None:
            output = {"error": str(exc_val)}
        end_trace(status, output)
        return False


class AsyncSpanContext:
    """异步 Span 上下文管理器。"""

    def __init__(self, name: str, input_data: Optional[dict] = None,
                 metadata: Optional[dict] = None):
        self.name = name
        self.input_data = input_data
        self.metadata = metadata
        self.span = None

    async def __aenter__(self):
        self.span = start_span(self.name, self.input_data, self.metadata)
        return self.span

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        status = "error" if exc_type is not None else "success"
        output = None
        error = None
        if exc_val is not None:
            error = exc_val
        end_span(status, output, error)
        return False
