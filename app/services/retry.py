"""全链路重试：指数退避，最多 3 次"""
import asyncio
from functools import wraps
from loguru import logger

def with_retry(max_retries: int = 3, base_delay: float = 2.0, exceptions=(Exception,)):
    """装饰器：指数退避重试。base_delay * 2^attempt（2s, 4s, 8s）"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_err = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"[Retry] {func.__name__} attempt {attempt+1}/{max_retries} failed: {e}, retry in {delay}s")
                        await asyncio.sleep(delay)
            logger.error(f"[Retry] {func.__name__} all {max_retries} attempts failed: {last_err}")
            raise last_err
        return wrapper
    return decorator
