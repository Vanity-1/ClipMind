"""链路追踪服务 - 为每个处理任务提供唯一 trace_id"""

import uuid
import asyncio
from contextvars import ContextVar
from loguru import logger
from typing import Optional

_trace_id_var: ContextVar[Optional[str]] = ContextVar('trace_id', default=None)
_step_stack_var: ContextVar[list[str]] = ContextVar('step_stack', default=[])
# operation_type：区分用户操作 (user_action) / 系统内部 (system_internal) / 后台任务 (background_task)
_operation_type_var: ContextVar[Optional[str]] = ContextVar('operation_type', default=None)

def get_trace_id() -> Optional[str]:
    return _trace_id_var.get()

def set_trace_id(trace_id: str) -> None:
    _trace_id_var.set(trace_id)

def generate_trace_id() -> str:
    trace_id = str(uuid.uuid4())
    set_trace_id(trace_id)
    return trace_id

def clear_trace_id() -> None:
    _trace_id_var.set(None)

def push_step(step: str) -> None:
    stack = _step_stack_var.get()
    stack.append(step)
    _step_stack_var.set(stack)

def pop_step() -> str:
    stack = _step_stack_var.get()
    if stack:
        return stack.pop()
    return ""

def get_current_step() -> str:
    stack = _step_stack_var.get()
    return stack[-1] if stack else ""

def get_operation_type() -> Optional[str]:
    """获取当前 operation_type；未设置时返回 None（调用方可回退到 system_internal）。"""
    return _operation_type_var.get()

def set_operation_type(op_type: str) -> None:
    """设置当前上下文的 operation_type 标签。"""
    _operation_type_var.set(op_type)

def _format_prefix() -> str:
    """构造 TraceLogger 消息前缀：[trace_id=xxx] [step=yyy] [op=zzz]"""
    trace_id = get_trace_id()
    extra = f"[trace_id={trace_id}] " if trace_id else ""
    current_step = get_current_step()
    step_extra = f"[step={current_step}] " if current_step else ""
    op_type = get_operation_type() or "system_internal"
    op_extra = f"[op={op_type}] "
    return f"{extra}{step_extra}{op_extra}"

class TraceLogger:

    @staticmethod
    def info(msg: str, **kwargs) -> None:
        logger.info(f"{_format_prefix()}{msg}", **kwargs)

    @staticmethod
    def error(msg: str, **kwargs) -> None:
        logger.error(f"{_format_prefix()}{msg}", **kwargs)

    @staticmethod
    def debug(msg: str, **kwargs) -> None:
        logger.debug(f"{_format_prefix()}{msg}", **kwargs)

    @staticmethod
    def warning(msg: str, **kwargs) -> None:
        logger.warning(f"{_format_prefix()}{msg}", **kwargs)

trace_logger = TraceLogger()

class TraceContext:

    def __init__(
        self,
        trace_id: Optional[str] = None,
        step: Optional[str] = None,
        operation_type: Optional[str] = None,
    ):
        self.trace_id = trace_id or str(uuid.uuid4())
        self.step = step
        self.operation_type = operation_type
        self._token = None
        self._step_token = None
        self._op_token = None

    def __enter__(self):
        self._token = _trace_id_var.set(self.trace_id)
        if self.step:
            stack = _step_stack_var.get()
            stack.append(self.step)
            _step_stack_var.set(stack)
            self._step_token = True
        if self.operation_type:
            self._op_token = _operation_type_var.set(self.operation_type)
        return self.trace_id

    def __exit__(self, *args):
        _trace_id_var.reset(self._token)
        if self._step_token:
            stack = _step_stack_var.get()
            if stack:
                stack.pop()
        if self._op_token is not None:
            _operation_type_var.reset(self._op_token)