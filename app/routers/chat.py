"""
Bilibili RAG 知识库系统
对话路由 - 智能问答
"""
import asyncio
import re
import json
import time
import uuid
from typing import Callable, List, Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import select, func, or_, delete
from sqlalchemy.ext.asyncio import AsyncSession
from openai import AsyncOpenAI
from langchain_core.documents import Document

from app.database import get_db, get_db_context
from app.models import (
    ChatRequest, ChatResponse, ChatSession, ChatMessage,
    FavoriteFolder, FavoriteVideo, VideoCache, UserSession,
)
from app.config import settings
from app.routers.knowledge import get_rag_service, _get_session_ids_for_user
from app.routers.auth import get_session
from app.services.tracing import TraceContext, trace_logger
from app.services.retrieval import (
    build_snippet,
    extract_keywords as extract_retrieval_keywords,
    keyword_score,
    merge_ranked_documents,
)
from app.services.chat_cache import get_cache, set_cache

router = APIRouter(prefix="/chat", tags=["对话"])
ProgressCallback = Optional[Callable[[dict], None]]

# LLM 客户端单例：避免每次请求新建连接池，减少握手开销
# 使用 _client_lock 保证并发初始化只创建一个实例
# 类型可能为 AsyncOpenAI（api 模式）或 ollama.AsyncClient（ollama 模式）
_async_llm_client: Optional[object] = None
_client_lock = asyncio.Lock()


def _llm_provider_mode() -> str:
    """读取当前 LLM Provider，规范化为小写。空值回退为 'api'。"""
    raw = getattr(settings, "llm_provider", "") or ""
    if not isinstance(raw, str):
        return "api"
    mode = raw.strip().lower()
    return mode or "api"


async def _get_async_llm_client():
    """获取异步 LLM 客户端（单例），并发安全。

    根据 settings.llm_provider 选择后端：
    - api（默认）：AsyncOpenAI，兼容 OpenAI / Azure / 第三方兼容 API
    - ollama：ollama.AsyncClient，本地 Ollama 服务
    """
    global _async_llm_client
    if _async_llm_client is not None:
        return _async_llm_client
    async with _client_lock:
        # double-check：进入锁后再次确认，避免多个协程都创建实例
        if _async_llm_client is not None:
            return _async_llm_client
        if _llm_provider_mode() == "ollama":
            try:
                from ollama import AsyncClient as _OllamaAsyncClient
            except ImportError as exc:
                logger.error("[Chat] 缺少 ollama 包，无法初始化 Ollama LLM 客户端")
                raise HTTPException(
                    status_code=500,
                    detail="需安装 ollama: pip install ollama",
                ) from exc
            _ollama_url = (getattr(settings, "ollama_base_url", "") or "").strip() or "http://localhost:11434"
            _async_llm_client = _OllamaAsyncClient(host=_ollama_url)
            logger.info(f"[Chat] 使用 Ollama AsyncClient (host={_ollama_url})")
        else:
            if not settings.openai_api_key:
                raise HTTPException(status_code=400, detail="未配置 LLM API Key")
            _async_llm_client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            )
    return _async_llm_client


def _current_llm_model() -> str:
    """根据 provider 返回当前生效的模型名。"""
    if _llm_provider_mode() == "ollama":
        return (getattr(settings, "ollama_model", "") or "").strip() or "qwen2.5:7b"
    return settings.llm_model


async def _llm_chat_complete(
    messages: list[dict],
    *,
    temperature: float = 0.5,
    timeout: int = 60,
) -> str:
    """统一的非流式 LLM 调用，兼容 OpenAI / Ollama。

    返回模型回复的文本内容。空响应抛 RuntimeError。
    """
    if _llm_provider_mode() == "ollama":
        client = await _get_async_llm_client()
        resp = await client.chat(
            model=_current_llm_model(),
            messages=messages,
            options={"temperature": temperature},
        )
        content = getattr(getattr(resp, "message", None), "content", None) or ""
        if not content:
            raise RuntimeError("LLM 返回空响应")
        return content
    client = await _get_async_llm_client()
    response = await client.chat.completions.create(
        model=_current_llm_model(),
        messages=messages,
        temperature=temperature,
        timeout=timeout,
    )
    if not response.choices:
        raise RuntimeError("LLM 返回空响应")
    return response.choices[0].message.content or ""


async def _llm_chat_stream(
    messages: list[dict],
    *,
    temperature: float = 0.5,
    timeout: int = 120,
):
    """统一的流式 LLM 调用，兼容 OpenAI / Ollama。

    yield 每个 token 字符串。底层 stream 在生成器结束时自动关闭。
    """
    if _llm_provider_mode() == "ollama":
        client = await _get_async_llm_client()
        stream = await client.chat(
            model=_current_llm_model(),
            messages=messages,
            stream=True,
            options={"temperature": temperature},
        )
        try:
            async for chunk in stream:
                content = getattr(getattr(chunk, "message", None), "content", None) or ""
                if content:
                    yield content
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as close_err:
                    logger.debug(f"ollama stream aclose error: {close_err}")
        return
    client = await _get_async_llm_client()
    stream = await client.chat.completions.create(
        model=_current_llm_model(),
        messages=messages,
        temperature=temperature,
        stream=True,
        timeout=timeout,
    )
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception as close_err:
                logger.debug(f"openai stream aclose error: {close_err}")

def _emit_progress(callback: ProgressCallback, event_type: str, **payload) -> None:
    if callback:
        callback({"type": event_type, **payload})

def _encode_stream_event(type: str, **payload) -> str:
    """编码一条 NDJSON 流式事件。"""
    return json.dumps({"type": type, **payload}, ensure_ascii=False) + "\n"

def _is_data_inspection_error(error: Exception) -> bool:
    """识别上游模型内容安全检查失败。"""
    text = str(error).lower()
    return "data_inspection_failed" in text or "datainspectionfailed" in text

def _data_inspection_error_message() -> str:
    return (
        "上游模型拒绝处理当前输入，通常是入库文本触发了模型服务的内容安全检查。"
        "可以尝试缩小提问范围、只问具体片段，或切换内容安全策略不同的模型服务。"
    )

def _route_label(route: str) -> str:
    return {
        "direct": "直接回答",
        "db_list": "读取收藏夹清单",
        "db_content": "汇总收藏夹内容",
        "vector": "检索知识库",
    }.get(route, route)

def _build_snippet_event(doc: Document) -> dict:
    meta = doc.metadata or {}
    preview = re.sub(r"\s+", " ", doc.page_content or "").strip()
    if len(preview) > 220:
        preview = preview[:220].rstrip() + "..."
    bvid = meta.get("bvid", "")
    # 按平台生成正确的视频 URL，避免抖音视频也写 bilibili URL
    if meta.get("url"):
        url = meta.get("url")
    elif not bvid:
        url = ""
    elif meta.get("platform") == "douyin":
        url = f"https://www.douyin.com/video/{bvid}"
    else:
        url = f"https://www.bilibili.com/video/{bvid}"
    return {
        "bvid": bvid,
        "title": meta.get("title", "") or bvid or "未命名视频",
        "preview": preview,
        "url": url,
    }

def _build_overview_messages(context: str, question: str) -> list[dict]:
    system = (
        "你是一个收藏夹知识库助手。用户想要了解他们收藏夹的整体内容。\n"
        "请根据以下视频信息回答用户的问题。回答要：\n"
        "1. 自然、友好、有条理\n"
        "2. 可以总结、分类、提炼要点\n"
        "3. 如果内容较多，挑选代表性的进行介绍\n\n"
        f"收藏夹内容：\n{context}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

def _build_rag_messages(context: str, question: str) -> list[dict]:
    system = (
        "你是一个知识库助手，基于用户收藏的视频内容回答问题。\n"
        "请根据以下检索到的视频内容回答：\n"
        "1. 直接回答问题，引用相关内容\n"
        "2. 回答要自然、有条理\n"
        "3. 可以引用视频标题作为来源\n\n"
        f"相关内容：\n{context}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

def _build_fallback_messages(context: str, question: str) -> list[dict]:
    system = (
        "你是一个收藏夹知识库助手。\n"
        "用户的问题在现有知识库中没有检索到直接内容。\n"
        "以下是用户收藏夹中的视频概览（如果为空说明用户还没入库）：\n"
        f"{context}\n\n"
        "请根据以上信息（如果有）：\n"
        "1. 尝试回答用户问题\n"
        "2. 如果没有任何视频信息，礼貌地告诉用户需要先在左侧选择收藏夹并点击「入库」或者「更新」\n"
        "3. 保持像真人助手一样的语气，不要显示这是\"备选方案\""
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

def _build_direct_messages(question: str) -> list[dict]:
    """通用回答（不查库）"""
    system = (
        "你是一个知识库问答助手。\n"
        "请直接回答用户问题，避免引入收藏夹或知识库内容。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

def _build_direct_messages_with_context(context: str, question: str) -> list[dict]:
    """带收藏夹上下文的通用回答（引导用户提问）"""
    system = (
        "你是一个知识库问答助手。\n"
        "以下是用户收藏夹的概览（收藏夹名称与视频标题）：\n"
        f"{context}\n\n"
        "请先回答用户问题，再根据收藏夹内容引导用户提出与收藏相关的问题。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

def _build_db_list_messages(context: str, question: str) -> list[dict]:
    """仅用标题/简介回答列表类问题"""
    system = (
        "你是一个收藏夹知识库助手。\n"
        "用户需要清单/列表类答案，请基于以下视频标题与简介回答。\n"
        "回答要：\n"
        "1. 按收藏夹或主题分组\n"
        "2. 只输出与问题相关的条目\n"
        "3. 简洁清晰\n\n"
        f"收藏夹内容：\n{context}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

def _build_db_summary_messages(context: str, question: str) -> list[dict]:
    """仅用数据库内容回答总结类问题"""
    system = (
        "你是一个收藏夹知识库助手。\n"
        "用户需要总结/提炼，请基于以下视频内容回答。\n"
        "回答要：\n"
        "1. 提炼重点与要点\n"
        "2. 结构清晰、可快速理解\n"
        "3. 必要时引用视频标题作为来源\n\n"
        f"收藏夹内容：\n{context}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

def _is_list_question(question: str) -> bool:
    """列表/清单类问题"""
    list_terms = ["有哪些", "有什么", "列表", "清单", "目录", "都有哪些", "列出", "罗列", "多少个", "几个"]
    return any(term in question for term in list_terms)

def _is_summary_question(question: str) -> bool:
    """总结/概括类问题"""
    summary_terms = ["总结", "概述", "概括", "分析", "梳理", "提炼", "回顾", "复盘", "要点", "重点", "关键点", "核心", "讲了什么", "讲些什么"]
    return any(term in question for term in summary_terms)

def _is_general_question(question: str) -> bool:
    """通用闲聊/与收藏无关的问题"""
    general_terms = [
        "你好", "嗨", "哈喽", "hello", "hi", "ok", "在吗", "你是谁", "你能做什么",
        "谢谢", "好的", "好", "收到", "明白", "可以", "嗯", "嗯嗯", "晚安", "早安", "早上好",
    ]
    cleaned = re.sub(r"[\W_]+", "", question, flags=re.UNICODE)
    lowered = cleaned.lower()
    residual = lowered
    for term in general_terms:
        residual = residual.replace(term.lower(), "")
    return residual == ""

def _is_collection_intent(question: str) -> bool:
    """是否显式指向收藏/视频/知识库"""
    terms = ["收藏", "收藏夹", "视频", "合集", "up主", "BV", "bv", "分P", "字幕", "知识库", "入库", "同步", "向量", "检索"]
    return any(term in question for term in terms)

def _is_overview_question(question: str) -> bool:
    """概览类问题（列表或总结）"""
    return _is_list_question(question) or _is_summary_question(question)

def _route_with_rules(question: str, is_collection_intent: bool, related: bool) -> str:
    """规则路由兜底"""
    if _is_general_question(question):
        return "direct"
    if _is_list_question(question):
        return "db_list"
    if _is_summary_question(question):
        return "db_content"
    if not related and not is_collection_intent:
        return "direct"
    return "vector"

async def _route_with_llm(question: str) -> tuple[Optional[str], str]:
    """使用 LLM 进行路由判断（异步，避免阻塞事件循环）"""
    try:
        system = (
            "你是一个路由器，只输出以下之一：direct, db_list, db_content, vector。\n"
            "规则：\n"
            "- direct：寒暄/闲聊/与收藏无关的问题\n"
            "- db_list：清单/列表/目录/有哪些\n"
            "- db_content：明确要求“全部/所有/整体/概览/全库”内容的总结\n"
            "- vector：具体主题问题或需要“先检索再总结”的问题\n"
            "示例：\n"
            "Q: 中西方文化的差异是什么，简单总结 -> vector\n"
            "Q: 概览我收藏夹里所有王德峰相关内容 -> db_content\n"
            "只输出一个词，不要解释。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]
        if _llm_provider_mode() == "ollama":
            client = await _get_async_llm_client()
            resp = await client.chat(
                model=_current_llm_model(),
                messages=messages,
                options={"temperature": 0},
            )
            text = (getattr(getattr(resp, "message", None), "content", None) or "").strip()
        else:
            client = await _get_async_llm_client()
            resp = await client.chat.completions.create(
                model=_current_llm_model(),
                messages=messages,
                temperature=0,
                timeout=30,
            )
            # 部分模型在内容安全拦截时返回空 choices，原代码直接 resp.choices[0] 会抛 IndexError
            if not resp.choices:
                logger.warning("LLM 路由返回空 choices，降级为规则路由")
                return None, ""
            text = (resp.choices[0].message.content or "").strip()
        match = re.search(r"(direct|db_list|db_content|vector)", text)
        return (match.group(1) if match else None), text
    except Exception as e:
        logger.warning(f"LLM 路由失败: {e}")
        return None, ""

def _escape_like_pattern(pattern: str) -> str:
    """转义 SQLite LIKE 模式中的通配符，避免用户输入的 % _ 被解释为通配符。

    SQLAlchemy 的 ilike 默认不转义，导致用户输入 "100%" 会匹配任意 100 后接任意字符。
    """
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_like_pattern(kw: str) -> str:
    """构建转义后的 LIKE 模式串。"""
    return f"%{_escape_like_pattern(kw)}%"


def _extract_keywords(question: str) -> List[str]:
    """提取用于过滤的关键词"""
    return extract_retrieval_keywords(question)

def _rank_sources_by_question(sources: List[dict], question: str) -> List[dict]:
    """让总结类回答的来源优先指向标题命中的视频。"""
    keywords = _extract_keywords(question)
    if not keywords:
        return sources
    ranked = []
    for index, source in enumerate(sources):
        score = keyword_score(keywords, title=source.get("title", "") or "")
        if score > 0:
            ranked.append((score, index, source))
    if not ranked:
        return sources
    ranked.sort(key=lambda item: (-item[0], item[1]))
    best_score = ranked[0][0]
    ranked = [item for item in ranked if item[0] >= best_score * 0.5]
    return [source for _, _, source in ranked]

def _build_keyword_document(
    *,
    bvid: str,
    title: str,
    description: Optional[str],
    content: Optional[str],
    owner_name: Optional[str],
    keywords: List[str],
    score: float,
    platform: Optional[str] = None,
) -> Document:
    """Build one DB keyword recall document for a video."""
    parts = [f"视频标题：{title}"]
    if owner_name:
        parts.append(f"UP主：{owner_name}")
    if description:
        parts.append(f"视频简介：{description}")
    if content:
        parts.append("相关片段：" + build_snippet(content, keywords))

    # 按平台生成正确的视频 URL
    if platform == "douyin":
        url = f"https://www.douyin.com/video/{bvid}"
    else:
        url = f"https://www.bilibili.com/video/{bvid}"

    return Document(
        page_content="\n".join(parts),
        metadata={
            "bvid": bvid,
            "title": title,
            "owner_name": owner_name or "",
            "platform": platform or "bilibili",
            "url": url,
            "doc_type": "keyword",
            "chunk_index": -2,
            "keyword_score": score,
        },
    )


async def _keyword_search_docs(
    db: AsyncSession,
    folder_ids: List[int],
    question: str,
    limit: int,
) -> List[Document]:
    """Recall videos from SQLite by weighted keyword matches."""
    if not folder_ids or limit <= 0:
        return []
    keywords = _extract_keywords(question)
    if not keywords:
        return []

    like_conds = []
    for kw in keywords:
        pattern = _build_like_pattern(kw)
        like_conds.extend([
            VideoCache.bvid.ilike(pattern, escape="\\"),
            VideoCache.title.ilike(pattern, escape="\\"),
            VideoCache.description.ilike(pattern, escape="\\"),
            VideoCache.content.ilike(pattern, escape="\\"),
            VideoCache.owner_name.ilike(pattern, escape="\\"),
        ])

    stmt = (
        select(
            VideoCache.bvid,
            VideoCache.title,
            VideoCache.description,
            VideoCache.content,
            VideoCache.owner_name,
            VideoCache.platform,
        )
        .join(FavoriteVideo, FavoriteVideo.bvid == VideoCache.bvid)
        .where(FavoriteVideo.folder_id.in_(folder_ids))
        .where(VideoCache.is_processed == True)
        .where(or_(*like_conds))
        .limit(max(limit * 4, 40))
    )
    rows = await db.execute(stmt)

    docs_by_bvid: dict[str, Document] = {}
    scores_by_bvid: dict[str, float] = {}
    for bvid, title, description, content, owner_name, platform in rows.fetchall():
        if not bvid or not title:
            continue
        score = keyword_score(
            keywords,
            title=title or "",
            description=description or "",
            content=content or "",
            owner_name=owner_name or "",
        )
        if score <= 0:
            continue
        if score <= scores_by_bvid.get(bvid, 0):
            continue
        scores_by_bvid[bvid] = score
        docs_by_bvid[bvid] = _build_keyword_document(
            bvid=bvid,
            title=title,
            description=description,
            content=content,
            owner_name=owner_name,
            keywords=keywords,
            score=score,
            platform=platform,
        )

    return sorted(
        docs_by_bvid.values(),
        key=lambda doc: doc.metadata.get("keyword_score", 0),
        reverse=True,
    )[:limit]

async def _is_related_to_collection(db: AsyncSession, folder_ids: List[int], question: str) -> bool:
    """判断问题是否与收藏夹内容有关"""
    if not folder_ids:
        return False
    keywords = _extract_keywords(question)
    if not keywords:
        return False
    like_conds = []
    for kw in keywords:
        pattern = _build_like_pattern(kw)
        like_conds.append(VideoCache.title.ilike(pattern, escape="\\"))
        like_conds.append(VideoCache.description.ilike(pattern, escape="\\"))
        like_conds.append(VideoCache.content.ilike(pattern, escape="\\"))
    stmt = (
        select(func.count())
        .select_from(VideoCache)
        .join(FavoriteVideo, FavoriteVideo.bvid == VideoCache.bvid)
        .where(FavoriteVideo.folder_id.in_(folder_ids))
        .where(or_(*like_conds))
    )
    count = await db.scalar(stmt)
    return (count or 0) > 0

async def _get_folder_ids_for_session(db: AsyncSession, session_id: str, media_ids: Optional[List[int]]) -> List[int]:
    """根据 session 和 media_id 获取内部 folder_id（支持跨 session 查找同用户数据）"""
    # 1. 尝试获取当前 session 的 mid（仅限 bilibili 平台）
    mid_result = await db.execute(select(UserSession.bili_mid).where(
        UserSession.session_id == session_id,
        UserSession.platform == "bilibili",
    ))
    mid = mid_result.scalar()
    target_session_ids = [session_id]
    if mid:
        # 查找该用户所有的 bilibili Session ID
        sessions_result = await db.execute(select(UserSession.session_id).where(
            UserSession.bili_mid == mid,
            UserSession.platform == "bilibili",
        ))
        target_session_ids = [row[0] for row in sessions_result.fetchall()]
    # 构建查询：按 media_id 去重，只保留最新的一条
    stmt = (
        select(FavoriteFolder.id, FavoriteFolder.media_id, FavoriteFolder.updated_at)
        .where(FavoriteFolder.session_id.in_(target_session_ids))
        .order_by(FavoriteFolder.updated_at.desc())
    )
    if media_ids:
        stmt = stmt.where(FavoriteFolder.media_id.in_(media_ids))
    rows = await db.execute(stmt)
    dedup: dict[int, int] = {}
    for folder_id, media_id, _updated_at in rows.fetchall():
        if media_id not in dedup:
            dedup[media_id] = folder_id
    return list(dedup.values())

async def _get_bvids_by_folder_ids(db: AsyncSession, folder_ids: List[int]) -> List[str]:
    """获取指定收藏夹的视频 BV 列表（仅返回已入库 is_processed=True 的视频）。

    过滤 is_processed 是为了保证传给 rag.search 的 bvids 只包含向量库中
    确实存在有效向量的视频，避免召回未入库或入库失败的残留数据。
    """
    if not folder_ids:
        return []
    rows = await db.execute(
        select(FavoriteVideo.bvid)
        .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid)
        .where(
            FavoriteVideo.folder_id.in_(folder_ids),
            VideoCache.is_processed == True,
        )
    )
    bvids = []
    seen = set()
    for (bvid,) in rows.fetchall():
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        bvids.append(bvid)
    return bvids

async def _get_video_context(db: AsyncSession, folder_ids: List[int], include_content: bool = False, limit: Optional[int] = 50, platform: Optional[str] = None) -> tuple[str, List[dict]]:
    """获取视频上下文信息"""
    if not folder_ids:
        return "", []
    # 查询视频信息
    query = (
        select(
            FavoriteFolder.title.label("folder_title"),
            VideoCache.bvid,
            VideoCache.title,
            VideoCache.description,
            VideoCache.platform,
            VideoCache.content if include_content else VideoCache.description,
        )
        .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
        .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid, isouter=True)
        .where(FavoriteFolder.id.in_(folder_ids))
    )
    if platform:
        query = query.where(VideoCache.platform == platform)
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    records = result.fetchall()
    if not records:
        return "", []
    # 按收藏夹分组（对 bvid 去重，避免同一视频重复出现）
    grouped = {}
    sources = []
    seen_bvids = set()
    for folder_title, bvid, title, desc, platform_val, content in records:
        if not bvid or not title:
            continue
        if bvid in seen_bvids:
            continue
        folder_name = folder_title or "默认收藏夹"
        if folder_name not in grouped:
            grouped[folder_name] = []
        video_info = f"- 《{title}》"
        if include_content and content:
            video_info += f"\n  摘要: {content}"
        elif desc:
            short_desc = desc[:100] + "..." if len(desc) > 100 else desc
            video_info += f" ({short_desc})"
        grouped[folder_name].append(video_info)
        seen_bvids.add(bvid)
        sources.append({
                    "bvid": bvid,
                    "title": title,
                    "url": (
                        f"https://www.douyin.com/video/{bvid}"
                        if platform_val == "douyin"
                        else f"https://www.bilibili.com/video/{bvid}"
                    ),
                })
    # 构建上下文文本
    context_parts = [f"【{folder_name}】\n" + "\n".join(videos) for folder_name, videos in grouped.items()]
    context = "\n\n".join(context_parts)
    return context, sources

async def _get_video_titles_context(db: AsyncSession, folder_ids: List[int], limit: int = 50, platform: Optional[str] = None) -> str:
    """获取收藏夹名称与视频标题（用于引导问题）"""
    if not folder_ids:
        return ""
    query = (
        select(FavoriteFolder.title.label("folder_title"), VideoCache.bvid, VideoCache.title)
        .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
        .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid, isouter=True)
        .where(FavoriteFolder.id.in_(folder_ids))
    )
    if platform:
        query = query.where(VideoCache.platform == platform)
    query = query.limit(limit)
    result = await db.execute(query)
    records = result.fetchall()
    if not records:
        return ""
    grouped = {}
    seen_bvids = set()
    for folder_title, bvid, title in records:
        if not title or not bvid:
            continue
        if bvid in seen_bvids:
            continue
        seen_bvids.add(bvid)
        folder_name = folder_title or "默认收藏夹"
        grouped.setdefault(folder_name, []).append(f"- 《{title}》")
    context_parts = [f"【{folder_name}】\n" + "\n".join(videos) for folder_name, videos in grouped.items()]
    return "\n\n".join(context_parts)

async def _prepare_messages(
    request: ChatRequest,
    db: AsyncSession,
    progress_callback: ProgressCallback = None,
) -> tuple[list[dict], List[dict], str]:
    """准备 LLM 消息与来源信息"""
    prepare_started = time.perf_counter()
    question = request.question.strip()
    folder_ids = []
    target_session_ids: List[str] = []
    if request.session_id:
        # 获取当前用户的所有 session_id（同 B 站账号），用于后续 platform 过滤的越权防护
        from app.models import UserSession
        mid_result = await db.execute(select(UserSession.bili_mid).where(
            UserSession.session_id == request.session_id,
            UserSession.platform == "bilibili",
        ))
        mid = mid_result.scalar()
        if mid:
            sessions_result = await db.execute(select(UserSession.session_id).where(
                UserSession.bili_mid == mid,
                UserSession.platform == "bilibili",
            ))
            target_session_ids = [row[0] for row in sessions_result.fetchall()]
        else:
            target_session_ids = [request.session_id]
        folder_ids = await _get_folder_ids_for_session(db, request.session_id, request.folder_ids)
        logger.info(f"Session: {request.session_id}, 关联 FolderIDs: {folder_ids}")
    if request.platform and request.platform != "all":
        # 安全修复：platform 过滤必须限定在当前用户的 session 范围内，
        # 否则会跨用户聚合 folder_ids 造成数据泄露
        plat_stmt = select(FavoriteFolder.id).where(FavoriteFolder.platform == request.platform)
        if target_session_ids:
            plat_stmt = plat_stmt.where(FavoriteFolder.session_id.in_(target_session_ids))
        result = await db.execute(plat_stmt)
        plat_ids = [row[0] for row in result.fetchall()]
        if plat_ids:
            folder_ids = list(set(folder_ids) | set(plat_ids))
            logger.info(f"Platform: {request.platform}, 附加 FolderIDs: {plat_ids}")
    platform_filter = request.platform
    bvids = await _get_bvids_by_folder_ids(db, folder_ids) if folder_ids else []
    _emit_progress(
        progress_callback,
        "scope",
        stage="scope",
        folder_count=len(folder_ids),
        video_count=len(bvids),
        message=f"已确定检索范围：{len(folder_ids)} 个收藏夹，共 {len(bvids)} 个视频",
    )
    has_data = len(bvids) > 0
    user_explicit_scope = bool(request.folder_ids) or (platform_filter and platform_filter != "all")
    is_collection_intent = _is_collection_intent(question)
    is_general = _is_general_question(question)
    if request.folder_ids and not is_general:
        is_collection_intent = True
    # 1) 默认使用规则路由，避免每次回答前额外等待一轮 LLM。
    route_started = time.perf_counter()
    logger.info(f"路由输入: question={question} folder_ids={folder_ids} has_data={has_data} is_collection_intent={is_collection_intent}")
    related: Optional[bool] = None
    route: Optional[str] = None
    route_source = "RULE"
    if settings.chat_use_llm_router and not is_general:
        route, _route_raw = await _route_with_llm(question)
        if route:
            route_source = "LLM"
    if not route:
        route = _route_with_rules(question, is_collection_intent, related=False)
        route_source = "RULE"
    logger.info(f"路由策略: {route_source} => {route}，耗时={time.perf_counter() - route_started:.2f}s")
    _emit_progress(
        progress_callback,
        "status",
        stage="routing",
        route=route,
        message=f"问题处理方式：{_route_label(route)}",
    )
    # 纠偏 (skip when platform filter active or user explicitly set folder_ids)
    if is_general and not (platform_filter and platform_filter != "all") and not bool(request.folder_ids):
        route = "direct"
    # 2) 无数据时处理 - unless user explicitly specified folder_ids or platform filter
    if not has_data and not user_explicit_scope:
        if platform_filter and platform_filter != "all":
            pass
        elif is_collection_intent:
            context, sources = await _get_video_context(db, folder_ids, include_content=False, limit=50)
            if not context:
                context = "（暂无已入库的视频信息，请提醒用户可能需要先进行入库操作）"
            messages = _build_fallback_messages(context, question)
            return messages, sources, question
        else:
            messages = _build_direct_messages(question)
            return messages, [], question
    # 3) 直接回答。非寒暄问题如果能在库里命中关键词，转入检索，避免被路由误杀。
    #    重要：用户显式指定 folder_ids 或 platform 过滤时，即使 general question 也继续走检索，
    #    确保用户主动限定的范围内能触发向量检索并发出 retrieval/snippet 进度事件。
    if route == "direct":
        if is_general and not user_explicit_scope:
            return _build_direct_messages(question), [], question
        if is_collection_intent:
            route = "vector"
            _emit_progress(
                progress_callback,
                "status",
                stage="routing",
                route=route,
                message=f"问题处理方式：{_route_label(route)}",
            )
        else:
            _emit_progress(
                progress_callback,
                "status",
                stage="relatedness",
                message="正在检查问题与知识库内容的关联",
            )
            if platform_filter and platform_filter != "all":
                route = "vector"
                _emit_progress(
                    progress_callback,
                    "status",
                    stage="routing",
                    route=route,
                    message=f"问题处理方式：{_route_label(route)}",
                )
            else:
                related = await _is_related_to_collection(db, folder_ids, question)
                if related:
                    route = "vector"
                    _emit_progress(
                        progress_callback,
                        "status",
                        stage="routing",
                        route=route,
                        message=f"问题处理方式：{_route_label(route)}",
                    )
                else:
                    title_context = await _get_video_titles_context(db, folder_ids, limit=50)
                    messages = _build_direct_messages_with_context(title_context, question) if title_context else _build_direct_messages(question)
                    return messages, [], question

    if route == "direct":
        title_context = await _get_video_titles_context(db, folder_ids, limit=50)
        messages = _build_direct_messages_with_context(title_context, question) if title_context else _build_direct_messages(question)
        return messages, [], question
    # 4) 列表类问题
    if route == "db_list":
        _emit_progress(progress_callback, "status", stage="retrieval", message="正在读取收藏夹视频清单")
        if related is None and not is_collection_intent:
            related = await _is_related_to_collection(db, folder_ids, question)
        if not related and not is_collection_intent:
            return _build_direct_messages(question), [], question
        context, sources = await _get_video_context(db, folder_ids, include_content=False, limit=50)
        _emit_progress(
            progress_callback,
            "retrieval",
            stage="retrieval",
            final_count=len(sources),
            message=f"已读取 {len(sources)} 个相关视频条目",
        )
        if not context:
            return _build_fallback_messages("（暂无信息，请入库）", question), sources, question
        return _build_db_list_messages(context, question), sources, question
    # 5) 总结类问题
    if route == "db_content":
        _emit_progress(progress_callback, "status", stage="retrieval", message="正在汇总已入库视频内容")
        if related is None and not is_collection_intent:
            related = await _is_related_to_collection(db, folder_ids, question)
        if not related and not is_collection_intent:
            return _build_direct_messages(question), [], question
        context, sources = await _get_video_context(db, folder_ids, include_content=True, limit=None)
        _emit_progress(
            progress_callback,
            "retrieval",
            stage="retrieval",
            final_count=len(sources),
            message=f"已读取 {len(sources)} 个视频的入库内容",
        )
        if not context:
            return _build_fallback_messages("（暂无信息，请入库）", question), sources, question
        sources = _rank_sources_by_question(sources, question)
        return _build_db_summary_messages(context, question), sources, question
    # 6) 检查相关性。vector 路由本身就是语义检索意图，不再用关键词 LIKE 提前拦截。
    if route != "vector":
        if related is None:
            related = await _is_related_to_collection(db, folder_ids, question)
        if not related and not is_collection_intent:
            return _build_direct_messages(question), [], question
    # 7) 混合检索：向量 MMR + SQLite 关键词召回，再用 RRF 融合。
    docs: List[Document] = []
    try:
        recall_started = time.perf_counter()
        _emit_progress(
            progress_callback,
            "status",
            stage="retrieval",
            message="正在并发执行向量检索与关键词检索",
        )
        top_k = max(1, settings.retrieval_top_k)
        candidate_k = max(top_k, settings.retrieval_candidate_k)
        async def _search_platforms():
            if platform_filter and platform_filter != "all":
                rag_inst = get_rag_service(platform_filter)
                return await asyncio.to_thread(
                    rag_inst.search, question, k=candidate_k,
                    bvids=bvids if bvids else None,
                    fetch_k=max(candidate_k, settings.retrieval_mmr_fetch_k),
                )
            else:
                # Search both collections in parallel
                t_bili = asyncio.to_thread(
                    get_rag_service("bilibili").search, question, k=candidate_k,
                    bvids=bvids if bvids else None,
                    fetch_k=max(candidate_k, settings.retrieval_mmr_fetch_k),
                )
                t_douyin = asyncio.to_thread(
                    get_rag_service("douyin").search, question, k=candidate_k,
                    bvids=bvids if bvids else None,
                    fetch_k=max(candidate_k, settings.retrieval_mmr_fetch_k),
                )
                bili_docs, douyin_docs = await asyncio.gather(t_bili, t_douyin)
                return bili_docs + douyin_docs
        vector_task = _search_platforms()
        keyword_task = _keyword_search_docs(db, folder_ids, question, limit=candidate_k)
        vector_docs, keyword_docs = await asyncio.gather(vector_task, keyword_task)
        docs = merge_ranked_documents(
            {"vector": vector_docs, "keyword": keyword_docs},
            top_k=top_k,
            channel_weights={"vector": 1.0, "keyword": 0.9},
            per_video_limit=2,
        )
        logger.info(
            f"混合检索完成：vector={len(vector_docs)} keyword={len(keyword_docs)} final={len(docs)}，耗时={time.perf_counter() - recall_started:.2f}s"
        )
        _emit_progress(
            progress_callback,
            "retrieval",
            stage="retrieval",
            vector_count=len(vector_docs),
            keyword_count=len(keyword_docs),
            final_count=len(docs),
            elapsed_ms=round((time.perf_counter() - recall_started) * 1000),
            message=f"检索完成：筛选出 {len(docs)} 个相关片段",
        )
        for doc in docs[:5]:
            _emit_progress(progress_callback, "snippet", stage="retrieval", **_build_snippet_event(doc))
    except Exception as e:
        logger.error(f"混合检索失败: {e}")
        raise RuntimeError("知识库检索失败") from e
    if docs:
        context_parts, sources, seen_bvids = [], [], set()
        for doc in docs:
            bvid, title, content = doc.metadata.get("bvid", ""), doc.metadata.get("title", ""), doc.page_content.strip()
            if content:
                context_parts.append(f"【{title}】\n{content}")
            if bvid and bvid not in seen_bvids:
                seen_bvids.add(bvid)
                sources.append({
                    "bvid": bvid,
                    "title": title,
                    "url": (
                        f"https://www.douyin.com/video/{bvid}"
                        if doc.metadata.get("platform") == "douyin"
                        else f"https://www.bilibili.com/video/{bvid}"
                    ),
                })
        logger.info(f"准备问答上下文完成，耗时={time.perf_counter() - prepare_started:.2f}s")
        return _build_rag_messages("\n\n---\n\n".join(context_parts), question), sources, question
    # 兜底
    context, sources = await _get_video_context(db, folder_ids, include_content=False, limit=50, platform=platform_filter)
    return _build_fallback_messages(context or "（暂无入库信息）", question), sources, question

async def _reconstruct_sources_by_bvids(db: AsyncSession, bvids: List[str]) -> List[dict]:
    """根据 bvid 列表从 VideoCache 反查视频元信息，重建 sources。

    问答缓存命中时只存了 doc_ids（bvid），需要在返回前重建
    ``{bvid, title, url}`` 结构以保持前端契约；保持缓存写入时的顺序。
    """
    if not bvids:
        return []
    rows = await db.execute(
        select(VideoCache.bvid, VideoCache.title, VideoCache.platform)
        .where(VideoCache.bvid.in_(bvids))
    )
    bvid_to_source: dict[str, dict] = {}
    for bvid, title, platform_val in rows.fetchall():
        if not bvid:
            continue
        bvid_to_source[bvid] = {
            "bvid": bvid,
            "title": title or bvid,
            "url": (
                f"https://www.douyin.com/video/{bvid}"
                if platform_val == "douyin"
                else f"https://www.bilibili.com/video/{bvid}"
            ),
        }
    # 按缓存写入顺序返回，丢失的视频（已被清理）跳过
    return [bvid_to_source[b] for b in bvids if b in bvid_to_source]


# ==================== 会话历史持久化 CRUD ====================


def _serialize_session(session: ChatSession) -> dict:
    """将 ChatSession ORM 对象序列化为响应字典。"""
    return {
        "id": session.id,
        "title": session.title,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _serialize_message(message: ChatMessage) -> dict:
    """将 ChatMessage ORM 对象序列化为响应字典，doc_ids 反序列化为 list。"""
    doc_ids: list = []
    if message.retrieved_doc_ids_json:
        try:
            parsed = json.loads(message.retrieved_doc_ids_json)
            if isinstance(parsed, list):
                doc_ids = parsed
        except (json.JSONDecodeError, TypeError):
            doc_ids = []
    return {
        "id": message.id,
        "session_id": message.session_id,
        "role": message.role,
        "content": message.content,
        "retrieved_doc_ids": doc_ids,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


@router.post("/sessions")
async def create_session(db: AsyncSession = Depends(get_db)):
    """新建一个对话会话，返回 {id, title, created_at, updated_at}。"""
    session_id = str(uuid.uuid4())
    session = ChatSession(id=session_id, title="新会话")
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _serialize_session(session)


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)):
    """返回所有会话列表，按 updated_at 降序排列。"""
    stmt = select(ChatSession).order_by(ChatSession.updated_at.desc())
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    return [_serialize_session(s) for s in sessions]


@router.put("/sessions/{session_id}")
async def rename_session(
    session_id: str,
    title: str = Query(..., description="新的会话标题"),
    db: AsyncSession = Depends(get_db),
):
    """更新指定会话的标题。会话不存在返回 404。"""
    stmt = select(ChatSession).where(ChatSession.id == session_id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.title = title
    await db.commit()
    await db.refresh(session)
    return _serialize_session(session)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """删除会话及其关联的所有消息。会话不存在返回 404。"""
    stmt = select(ChatSession).where(ChatSession.id == session_id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 先删除关联消息，再删除会话本身
    await db.execute(
        delete(ChatMessage).where(ChatMessage.session_id == session_id)
    )
    await db.delete(session)
    await db.commit()
    return {"deleted": True, "id": session_id}


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """返回指定会话的全部消息，按 created_at 升序排列。"""
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    result = await db.execute(stmt)
    messages = result.scalars().all()
    return [_serialize_message(m) for m in messages]


async def _persist_chat_message(
    db: AsyncSession,
    session_id: str,
    role: str,
    content: str,
    retrieved_doc_ids: Optional[list] = None,
) -> None:
    """将一条问答消息写入 ChatMessage，并刷新 ChatSession.updated_at。

    失败仅告警，不影响主问答流程。调用方需在自身事务上下文中调用。
    """
    try:
        doc_ids_json = (
            json.dumps(retrieved_doc_ids, ensure_ascii=False)
            if retrieved_doc_ids
            else None
        )
        db.add(
            ChatMessage(
                session_id=session_id,
                role=role,
                content=content,
                retrieved_doc_ids_json=doc_ids_json,
            )
        )
        # 手动刷新 updated_at：onupdate 在某些 SQLite 驱动下不会立即回填到对象
        sess_result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        sess_row = sess_result.scalar_one_or_none()
        if sess_row is not None:
            from app.models import utcnow
            sess_row.updated_at = utcnow()
        await db.commit()
    except Exception as e:
        logger.warning(f"写入会话消息失败 (session_id={session_id}, role={role}): {e}")
        try:
            await db.rollback()
        except Exception:
            pass


@router.post("/ask", response_model=ChatResponse)
async def ask_question(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    """智能问答"""
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    trace_ctx = TraceContext(step=f"chat_ask:{request.question[:30]}")
    trace_ctx.__enter__()
    trace_logger.info(
        f"问答请求: session_id={request.session_id}, platform={request.platform}, "
        f"question_len={len(request.question)}"
    )
    # 校验 chat_session_id 是否存在（提供时），不存在直接 404，避免写入孤儿消息
    if request.chat_session_id:
        cs_result = await db.execute(
            select(ChatSession).where(ChatSession.id == request.chat_session_id)
        )
        if cs_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="chat_session 不存在")
    try:
        # 缓存命中：直接返回历史答案，跳过 LLM 调用
        try:
            cached = await get_cache(db, request.question)
            if cached is not None:
                trace_logger.info(
                    f"问答缓存命中 /ask，provider={cached.get('llm_provider')}"
                )
                cached_sources = await _reconstruct_sources_by_bvids(
                    db, cached.get("retrieved_doc_ids") or []
                )
                cached_answer = cached["answer"]
                cached_doc_ids = cached.get("retrieved_doc_ids") or []
                # 缓存命中也需要把问答写入会话历史（若提供了 chat_session_id）
                if request.chat_session_id:
                    await _persist_chat_message(
                        db, request.chat_session_id, "user", request.question
                    )
                    await _persist_chat_message(
                        db, request.chat_session_id, "assistant",
                        cached_answer, cached_doc_ids,
                    )
                return ChatResponse(
                    answer=cached_answer, sources=cached_sources[:5]
                )
        except Exception as cache_err:
            # 缓存读取失败不阻塞主流程，按未命中处理
            logger.warning(f"读取问答缓存失败: {cache_err}")

        # 提供了 chat_session_id 时先把用户问题写入历史
        if request.chat_session_id:
            await _persist_chat_message(
                db, request.chat_session_id, "user", request.question
            )

        messages, sources, _ = await _prepare_messages(request, db)
        # 兼容 Ollama / OpenAI 两种 provider：用统一封装的 _llm_chat_complete
        try:
            answer_text = await _llm_chat_complete(messages, temperature=0.5, timeout=60)
        except RuntimeError as e:
            # 空响应（OpenAI 返回空 choices 或 Ollama 返回空 content）
            trace_logger.warning(f"LLM 返回空响应: {e}")
            raise HTTPException(status_code=502, detail="LLM 返回空响应，请稍后重试")
        trace_logger.info(f"问答完成: sources_count={len(sources)}")
        # 写入缓存：失败仅告警，不影响响应
        if answer_text:
            try:
                doc_ids = [s.get("bvid") for s in sources if s.get("bvid")]
                await set_cache(
                    db, request.question, answer_text, doc_ids, _current_llm_model()
                )
            except Exception as cache_err:
                logger.warning(f"写入问答缓存失败: {cache_err}")
        # 把 assistant 回答写入会话历史（提供 chat_session_id 时）
        if request.chat_session_id and answer_text:
            assistant_doc_ids = [s.get("bvid") for s in sources if s.get("bvid")]
            await _persist_chat_message(
                db, request.chat_session_id, "assistant",
                answer_text, assistant_doc_ids,
            )
        return ChatResponse(answer=answer_text, sources=sources[:5])
    except HTTPException:
        raise
    except Exception as e:
        trace_logger.error(f"问答失败: {e}")
        logger.error(f"问答失败: {e}")
        if _is_data_inspection_error(e):
            raise HTTPException(status_code=422, detail=_data_inspection_error_message())
        raise HTTPException(status_code=500, detail="问答失败，请稍后重试")
    finally:
        trace_ctx.__exit__(None, None, None)

@router.post("/ask/stream")
async def ask_question_stream(request: ChatRequest):
    """以 NDJSON 事件流返回执行过程与答案。"""
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")

    # 入口日志（不在 trace context 中，仅记录请求到达）
    logger.info(
        f"流式问答请求到达: session_id={request.session_id}, platform={request.platform}, "
        f"question_len={len(request.question)}"
    )
    # 校验 chat_session_id 是否存在（提供时），不存在直接 404，避免写入孤儿消息
    if request.chat_session_id:
        async with get_db_context() as verify_db:
            cs_result = await verify_db.execute(
                select(ChatSession).where(ChatSession.id == request.chat_session_id)
            )
            if cs_result.scalar_one_or_none() is None:
                raise HTTPException(status_code=404, detail="chat_session 不存在")
    # trace_ctx 在 generate() 内部进入/退出，
    # 因为 StreamingResponse 返回后路由协程立即结束，
    # 真正的流式生成在 Starlette 后续迭代中执行，需保证 trace_id 覆盖整个生成过程。
    trace_step = f"chat_ask_stream:{request.question[:30]}"

    async def generate():
        trace_ctx = TraceContext(step=trace_step)
        trace_ctx.__enter__()
        trace_logger.info(f"开始流式问答生成: session_id={request.session_id}")
        try:
            # 缓存命中检查：命中则把缓存的 answer 按字符分块 yield，模拟流式输出
            try:
                async with get_db_context() as cache_db:
                    cached = await get_cache(cache_db, request.question)
                    if cached is not None:
                        cached_sources = await _reconstruct_sources_by_bvids(
                            cache_db, cached.get("retrieved_doc_ids") or []
                        )
            except Exception as cache_err:
                logger.warning(f"读取问答缓存失败: {cache_err}")
                cached = None
                cached_sources = []

            if cached is not None:
                trace_logger.info(
                    f"问答缓存命中 /ask/stream，provider={cached.get('llm_provider')}"
                )
                yield _encode_stream_event(
                    "status", stage="cache_hit", message="命中问答缓存，秒回"
                )
                yield _encode_stream_event("sources", items=cached_sources[:5])
                # 按字符分块 yield 缓存答案，模拟流式输出体验
                cached_answer = cached["answer"]
                chunk_size = 4
                for i in range(0, len(cached_answer), chunk_size):
                    yield _encode_stream_event(
                        "token", content=cached_answer[i:i + chunk_size]
                    )
                    # 让出事件循环，避免长答案独占协程
                    await asyncio.sleep(0)
                # 缓存命中也写入会话历史（提供 chat_session_id 时）
                if request.chat_session_id:
                    cached_doc_ids = cached.get("retrieved_doc_ids") or []
                    async with get_db_context() as hist_db:
                        await _persist_chat_message(
                            hist_db, request.chat_session_id, "user", request.question
                        )
                        await _persist_chat_message(
                            hist_db, request.chat_session_id, "assistant",
                            cached_answer, cached_doc_ids,
                        )
                yield _encode_stream_event("done")
                return

            # 缓存未命中：走正常检索 + LLM 流式生成
            yield _encode_stream_event("status", stage="routing", message="正在分析问题")
            progress_queue: asyncio.Queue[dict] = asyncio.Queue()
            prepare_task: Optional[asyncio.Task] = None

            def report(event: dict) -> None:
                progress_queue.put_nowait(event)

            # 提供了 chat_session_id 时先把用户问题写入历史
            if request.chat_session_id:
                async with get_db_context() as hist_db:
                    await _persist_chat_message(
                        hist_db, request.chat_session_id, "user", request.question
                    )

            try:
                async with get_db_context() as stream_db:
                    try:
                        prepare_task = asyncio.create_task(
                            _prepare_messages(request, stream_db, progress_callback=report)
                        )
                        while not prepare_task.done():
                            try:
                                event = await asyncio.wait_for(progress_queue.get(), timeout=0.1)
                                yield _encode_stream_event(**event)
                            except asyncio.TimeoutError:
                                continue
                        while not progress_queue.empty():
                            yield _encode_stream_event(**progress_queue.get_nowait())
                        messages, sources, _ = await prepare_task
                    finally:
                        if prepare_task and not prepare_task.done():
                            prepare_task.cancel()
                            await asyncio.gather(prepare_task, return_exceptions=True)

                yield _encode_stream_event("sources", items=sources[:5])
                yield _encode_stream_event("status", stage="generation", message="正在基于检索结果生成回答")

                # 兼容 Ollama / OpenAI 两种 provider：用统一封装的 _llm_chat_stream
                # 底层 stream 在生成器结束时自动关闭（含 aclose 兜底）
                emitted_content = False
                answer_parts: list[str] = []  # 收集完整答案用于写缓存
                async for token in _llm_chat_stream(messages, temperature=0.5, timeout=120):
                    if token:
                        emitted_content = True
                        answer_parts.append(token)
                        yield _encode_stream_event("token", content=token)
                if not emitted_content:
                    raise RuntimeError("AI 回答返回空结果")
                trace_logger.info(f"流式问答完成: sources_count={len(sources)}")
                # 写入缓存：失败仅告警，不影响响应
                full_answer = "".join(answer_parts)
                if full_answer:
                    try:
                        doc_ids = [s.get("bvid") for s in sources if s.get("bvid")]
                        async with get_db_context() as cache_db:
                            await set_cache(
                                cache_db,
                                request.question,
                                full_answer,
                                doc_ids,
                                _current_llm_model(),
                            )
                    except Exception as cache_err:
                        logger.warning(f"写入问答缓存失败: {cache_err}")
                # 把 assistant 回答写入会话历史（提供 chat_session_id 时）
                if request.chat_session_id and full_answer:
                    assistant_doc_ids = [s.get("bvid") for s in sources if s.get("bvid")]
                    async with get_db_context() as hist_db:
                        await _persist_chat_message(
                            hist_db, request.chat_session_id, "assistant",
                            full_answer, assistant_doc_ids,
                        )
                yield _encode_stream_event("done")
            except Exception as e:
                trace_logger.error(f"流式问答失败: {e}")
                logger.error(f"流式问答失败: {e}")
                if _is_data_inspection_error(e):
                    yield _encode_stream_event("error", message=_data_inspection_error_message())
                    return
                yield _encode_stream_event("error", message=f"问答失败: {e}")
        finally:
            trace_ctx.__exit__(None, None, None)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"X-Accel-Buffering": "no"},
    )

@router.post("/search")
async def search_videos(
    query: str,
    session_id: str = Query(..., description="会话ID，需为有效会话"),
    k: int = Query(5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """搜索相关视频片段

    并发检索 bilibili 与 douyin 两个集合后合并去重。
    安全：仅返回当前 session 可见的视频（按 folder 归属过滤），避免跨用户数据泄露。
    """
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="查询不能为空")
    trace_ctx = TraceContext(step=f"chat_search:{query[:30]}")
    trace_ctx.__enter__()
    trace_logger.info(f"视频搜索: session_id={session_id}, query_len={len(query)}, k={k}")
    session = await get_session(session_id, platform="bilibili")
    if not session:
        trace_logger.warning(f"未登录或会话已过期: session_id={session_id}")
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    try:
        # 收集当前用户可见的 bvid 集合，用于过滤搜索结果
        # 同时过滤 is_processed=True，避免召回未入库或入库失败的视频向量残留
        target_session_ids = await _get_session_ids_for_user(db, session_id)
        # B 站可见 bvid（按 session_id 维度，仅已入库）
        bili_bvids_rows = await db.execute(
            select(FavoriteVideo.bvid)
            .join(FavoriteFolder, FavoriteFolder.id == FavoriteVideo.folder_id)
            .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid)
            .where(
                FavoriteFolder.session_id.in_(target_session_ids),
                VideoCache.is_processed == True,
            )
        )
        bili_bvids = {r[0] for r in bili_bvids_rows.fetchall() if r[0]}
        # 抖音可见 bvid（按当前 session_id 派生 user_scope，仅已入库）
        douyin_bvids_rows = await db.execute(
            select(FavoriteVideo.bvid)
            .join(FavoriteFolder, FavoriteFolder.id == FavoriteVideo.folder_id)
            .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid)
            .where(
                FavoriteFolder.session_id.like(f"douyin-{session_id}-%"),
                VideoCache.is_processed == True,
            )
        )
        douyin_bvids = {r[0] for r in douyin_bvids_rows.fetchall() if r[0]}

        rag_bili = get_rag_service("bilibili")
        rag_douyin = get_rag_service("douyin")
        # 并发检索两个集合，单集合取 k 个保证每集合召回充分
        # 传入 bvids 过滤，确保只召回已入库的有效视频
        docs_bili, docs_douyin = await asyncio.gather(
            asyncio.to_thread(
                rag_bili.search, query, k=k,
                bvids=list(bili_bvids) if bili_bvids else None,
            ),
            asyncio.to_thread(
                rag_douyin.search, query, k=k,
                bvids=list(douyin_bvids) if douyin_bvids else None,
            ),
            return_exceptions=True,
        )
        # 容错：单个集合失败不影响另一集合结果
        all_docs: list = []
        for part in (docs_bili, docs_douyin):
            if isinstance(part, Exception):
                logger.warning(f"搜索部分失败: {type(part).__name__}: {part}")
                continue
            all_docs.extend(part)

        results, seen_bvids = [], set()
        for doc in all_docs:
            bvid = doc.metadata.get("bvid", "")
            if bvid in seen_bvids:
                continue
            # 安全校验：仅返回当前用户可见的视频
            platform = doc.metadata.get("platform", "bilibili")
            if platform == "douyin":
                if bvid not in douyin_bvids:
                    continue
            else:
                if bvid not in bili_bvids:
                    continue
            seen_bvids.add(bvid)
            results.append({
                "bvid": bvid,
                "title": doc.metadata.get("title", ""),
                "url": doc.metadata.get("url", ""),
                "content_preview": doc.page_content[:200] + "..." if len(doc.page_content) > 200 else doc.page_content
            })
        trace_logger.info(
            f"视频搜索完成: bili_candidates={len(bili_bvids)}, "
            f"douyin_candidates={len(douyin_bvids)}, results={len(results)}"
        )
        return {"results": results}
    except HTTPException:
        raise
    except Exception as e:
        trace_logger.error(f"搜索失败: {e}")
        logger.error(f"搜索失败: {e}")
        raise HTTPException(status_code=500, detail="搜索失败，请稍后重试")
    finally:
        trace_ctx.__exit__(None, None, None)
