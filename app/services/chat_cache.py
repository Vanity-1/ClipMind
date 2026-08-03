"""问答缓存服务。

命中缓存时跳过 LLM 调用，直接返回历史答案，实现重复提问秒回。
TTL 由 ``CHAT_CACHE_TTL_SEC`` 控制（默认 24 小时），过期记录在
``get_cache`` 命中时惰性删除，避免额外的后台清理任务。
"""
import hashlib
import json
import re
from typing import Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatCache, utcnow

# 缓存有效期：24 小时
CHAT_CACHE_TTL_SEC = 24 * 3600


def _normalize_question(q: str) -> str:
    """归一化问题：去首尾空格、转小写、去中英文标点与空白。

    归一化后再哈希，可以让 "你好？" / "你好" / " 你好。" 等价命中。
    """
    q = q.strip().lower()
    q = re.sub(r"[，。！？、,.!?;:；：\s]+", "", q)
    return q


def _hash_question(q: str) -> str:
    """对归一化后的问题做 sha256，返回 hex 摘要。"""
    return hashlib.sha256(_normalize_question(q).encode("utf-8")).hexdigest()


async def get_cache(db: AsyncSession, question: str) -> Optional[dict]:
    """查询缓存。

    命中且未过期返回 ``{answer, retrieved_doc_ids, llm_provider}``；
    不存在或已过期返回 ``None``。过期记录会被惰性删除。
    """
    q_hash = _hash_question(question)
    stmt = select(ChatCache).where(ChatCache.question_hash == q_hash)
    result = await db.execute(stmt)
    cache_row = result.scalar_one_or_none()
    if cache_row is None:
        return None
    # 检查 TTL：naive datetime 直接相减得到秒数
    age = (utcnow() - cache_row.created_at).total_seconds()
    if age > CHAT_CACHE_TTL_SEC:
        # 过期：删除该条记录，避免缓存无限膨胀
        try:
            await db.delete(cache_row)
            await db.commit()
        except Exception:
            # 删除失败不阻塞主流程，下次查询时再尝试清理
            await db.rollback()
        return None
    # 解析 doc_ids JSON，损坏数据降级为空列表
    retrieved_doc_ids: list = []
    if cache_row.retrieved_doc_ids_json:
        try:
            parsed = json.loads(cache_row.retrieved_doc_ids_json)
            if isinstance(parsed, list):
                retrieved_doc_ids = parsed
        except (json.JSONDecodeError, TypeError):
            retrieved_doc_ids = []
    return {
        "answer": cache_row.answer,
        "retrieved_doc_ids": retrieved_doc_ids,
        "llm_provider": cache_row.llm_provider,
    }


async def set_cache(
    db: AsyncSession,
    question: str,
    answer: str,
    retrieved_doc_ids: list,
    llm_provider: str,
) -> None:
    """写入缓存。已存在则更新答案与时间戳（幂等 upsert）。

    ``llm_provider`` 截断为 32 字符以匹配 ``ChatCache.llm_provider`` 列长度。
    """
    q_hash = _hash_question(question)
    stmt = select(ChatCache).where(ChatCache.question_hash == q_hash)
    result = await db.execute(stmt)
    cache_row = result.scalar_one_or_none()
    doc_ids_json = json.dumps(retrieved_doc_ids or [], ensure_ascii=False)
    provider_val = (llm_provider or "")[:32]
    if cache_row is None:
        cache_row = ChatCache(
            question_hash=q_hash,
            question=question,
            answer=answer,
            retrieved_doc_ids_json=doc_ids_json,
            llm_provider=provider_val,
        )
        db.add(cache_row)
    else:
        # 已存在：更新内容并刷新 created_at 以续期 TTL
        cache_row.question = question
        cache_row.answer = answer
        cache_row.retrieved_doc_ids_json = doc_ids_json
        cache_row.llm_provider = provider_val
        cache_row.created_at = utcnow()
    await db.commit()


async def invalidate_cache(db: AsyncSession, question: str) -> None:
    """删除指定问题的缓存。"""
    q_hash = _hash_question(question)
    stmt = delete(ChatCache).where(ChatCache.question_hash == q_hash)
    await db.execute(stmt)
    await db.commit()
