"""
ClipMind -- Douyin router

Phase 1: Manual share-link input -> ASR -> vector ingestion.
"""
import asyncio
import json
from datetime import datetime
from typing import Optional, List, Callable
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Depends, BackgroundTasks
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.database import get_db, get_db_context
from app.models import FavoriteFolder, FavoriteVideo, VideoCache, UserSession, Platform
from app.services.platform.douyin import DouyinPlatformService
from app.services.asr import ASRService
from app.services.rag import RAGService
from app.services.error_classifier import classify_error, is_transient, is_permanent, ErrorStage
from app.services.task_tracker import (
    BuildStatus,
    task_info_to_build_status,
)
from app.services.task_tracker_service import task_tracker, TaskStatus
from app.services.tracing import TraceContext, trace_logger

router = APIRouter(prefix="/douyin", tags=["抖音"])


def _record_cache_error(cache: Optional[VideoCache], error: Exception) -> None:
    """记录抖音视频处理错误详情到 VideoCache。"""
    if cache is None:
        return
    stage = classify_error(error)
    cache.is_processed = False
    cache.process_error = str(error)
    cache.last_error_stage = stage.value
    cache.last_error_detail = f"{type(error).__name__}: {str(error)}"
    cache.retry_count = (cache.retry_count or 0) + 1
    cache.permanent_failure = is_permanent(stage)
    trace_logger.error(
        "抖音视频处理失败: bvid={} stage={} retry_count={} permanent={} error={}",
        cache.bvid, stage.value, cache.retry_count, cache.permanent_failure, str(error),
    )


def _clear_cache_error(cache: Optional[VideoCache]) -> None:
    """入库成功时清除所有错误字段。"""
    if cache is None:
        return
    cache.is_processed = True
    cache.process_error = None
    cache.last_error_stage = None
    cache.last_error_detail = None
    cache.permanent_failure = False


# ---------------------------------------------------------------------------
#  Schemas
# ---------------------------------------------------------------------------

class ParseRequest(BaseModel):
    """Parse a Douyin share URL."""
    url: str


class ParseResponse(BaseModel):
    """Video info from share URL parsing."""
    video_id: str
    title: str
    description: str
    author: str
    cover_url: str
    duration: int


class IngestRequest(BaseModel):
    """Ingest a single Douyin video into the knowledge base."""
    video_id: str
    title: str = ""
    description: str = ""
    author: str = ""
    duration: int = 0
    cover_url: str = ""


class IngestResponse(BaseModel):
    video_id: str
    title: str
    message: str


class VideoItem(BaseModel):
    video_id: str
    title: str
    author: str
    duration: int
    content_source: Optional[str] = None
    is_processed: bool
    created_at: Optional[datetime] = None


class VideoListResponse(BaseModel):
    total: int
    videos: list[VideoItem]


# ---------------------------------------------------------------------------
#  Router-level RAG service (shared with knowledge module)
# ---------------------------------------------------------------------------


def get_rag() -> RAGService:
    from app.routers.knowledge import get_rag_service
    return get_rag_service("douyin")


# ---------------------------------------------------------------------------
#  Session helpers
# ---------------------------------------------------------------------------

# 抖音域名白名单（防止 SSRF）：仅允许抖音官方域名
_DOUYIN_ALLOWED_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "www.iesdouyin.com",
    "iesdouyin.com",
}


def _is_douyin_url(url: str) -> bool:
    """校验 URL 是否为抖音官方域名（防止 SSRF）。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host in _DOUYIN_ALLOWED_HOSTS or host.endswith(".douyin.com") or host.endswith(".iesdouyin.com")


async def _require_douyin_session(session_id: Optional[str], db: AsyncSession) -> tuple[str, str]:
    """校验抖音会话并返回 (session_id, user_scope)。

    安全策略：
    - 优先使用调用方显式传入的 session_id，校验 DB 中有效且 platform=douyin；
    - 若未传入，回退到 douyin-active 内存缓存；
    - 内存未命中时回退到 DB 查询最新的抖音有效 session，并重建内存缓存；
    - 严格校验 platform=douyin，防止 B站 session 被误用于抖音操作。
    """
    from app.routers.auth import login_sessions
    from app.services.crypto import decrypt_secret

    # 1. 优先使用传入的 session_id
    if session_id:
        # 先查内存缓存
        c = login_sessions.get(session_id) or {}
        if c.get("session_id") and c.get("platform") == "douyin":
            return session_id, session_id
        # 内存未命中，查 DB
        row = await db.scalar(
            select(UserSession).where(
                UserSession.session_id == session_id,
                UserSession.platform == Platform.DOUYIN,
                UserSession.is_valid.is_(True),
            )
        )
        if row:
            # 重建内存缓存
            login_sessions[session_id] = {
                "session_id": row.session_id,
                "douyin_cookie": decrypt_secret(row.douyin_cookie) if row.douyin_cookie else None,
                "douyin_uid": row.douyin_uid,
                "platform": Platform.DOUYIN,
            }
            return session_id, session_id
        # 传入的 session_id 已失效，回退到 douyin-active 内存缓存继续查找
        # （避免前端持有过期 session_id 时直接 401 无法回退）

    # 2. 回退到 douyin-active 内存缓存
    c = login_sessions.get("douyin-active") or {}
    active_sid = c.get("session_id")
    if active_sid and c.get("platform") == "douyin":
        return active_sid, active_sid

    # 3. 内存未命中，回退到 DB 查询最新的抖音有效 session
    row = await db.scalar(
        select(UserSession).where(
            UserSession.platform == Platform.DOUYIN,
            UserSession.is_valid.is_(True),
        ).order_by(UserSession.updated_at.desc())
    )
    if row:
        # 重建内存缓存
        login_sessions["douyin-active"] = {
            "session_id": row.session_id,
            "douyin_cookie": decrypt_secret(row.douyin_cookie) if row.douyin_cookie else None,
            "douyin_uid": row.douyin_uid,
            "platform": Platform.DOUYIN,
        }
        return row.session_id, row.session_id

    raise HTTPException(status_code=401, detail="未登录抖音或会话已过期，请重新登录")


async def _verify_folder_ownership(db: AsyncSession, folder_id: int, user_scope: str) -> FavoriteFolder:
    """校验文件夹归属当前用户，返回 folder 对象。"""
    folder = await db.scalar(
        select(FavoriteFolder).where(
            FavoriteFolder.id == folder_id,
            FavoriteFolder.platform == "douyin",
            FavoriteFolder.session_id.like(f"douyin-{user_scope}-%"),
        )
    )
    if not folder:
        raise HTTPException(status_code=404, detail="文件夹不存在或无权访问")
    return folder


# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------

@router.post("/parse", response_model=ParseResponse)
async def parse_douyin_url(
    payload: ParseRequest,
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a Douyin share link and return video metadata.

    B9: 增加域名白名单校验防止 SSRF。
    """
    raw = (payload.url or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="链接不能为空")
    if not _is_douyin_url(raw):
        raise HTTPException(status_code=400, detail="仅支持抖音官方域名链接")

    if session_id:
        await _require_douyin_session(session_id, db)

    douyin = DouyinPlatformService()
    try:
        result = await douyin.parse_share_url(raw)
        if not result:
            raise HTTPException(status_code=400, detail="无法解析该链接，请检查链接是否正确")
        return ParseResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"解析 Douyin 链接失败: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="解析失败，请稍后重试")
    finally:
        await douyin.close()


# ---------------------------------------------------------------------------
#  文件夹列表 & 视频详情
# ---------------------------------------------------------------------------

class DouyinFolderInfo(BaseModel):
    folder_id: int
    title: str
    media_count: int
    platform: str
    indexed_count: int
    status: str  # "all_indexed" | "partial" | "none"
    is_selected: Optional[bool] = False

class DouyinFolderVideo(BaseModel):
    video_id: str
    title: str
    author: str
    duration: int
    is_processed: bool
    is_selected: bool

class DouyinFolderVideosResponse(BaseModel):
    folder_id: int
    folder_title: str
    videos: list[DouyinFolderVideo]
    total: int
    indexed_count: int


@router.get("/folders/list", response_model=List[DouyinFolderInfo])
async def list_douyin_folders(
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """List Douyin favorite folders with index status.

    按当前抖音登录用户隔离文件夹，确保不同用户互不可见。
    """
    _, user_scope = await _require_douyin_session(session_id, db)

    folders = (await db.execute(
        select(FavoriteFolder).where(
            FavoriteFolder.platform == "douyin",
            FavoriteFolder.session_id.like(f"douyin-{user_scope}-%"),
            FavoriteFolder.title.notin_(["抖音收藏-我的文件夹"]),
        )
    )).scalars().all()

    result = []
    for f in folders:
        # 优化 N+1 查询：一次性取该 folder 下的 FavoriteVideo 与 VideoCache 关联
        links = (await db.execute(
            select(FavoriteVideo.bvid, VideoCache.is_processed)
            .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid, isouter=True)
            .where(
                FavoriteVideo.folder_id == f.id,
                VideoCache.platform == "douyin",
            )
        )).all()
        total = len(links)
        indexed = sum(1 for _, is_proc in links if is_proc)

        if indexed == 0 or total == 0:
            status = "none"
        elif indexed >= total:
            status = "all_indexed"
        else:
            status = "partial"

        result.append({
            "folder_id": f.id,
            "title": f.title or "",
            "media_count": f.media_count or 0,
            "platform": f.platform or "douyin",
            "indexed_count": indexed,
            "status": status,
            "is_selected": bool(f.is_selected),
        })
    return result


@router.get("/folders/{folder_id}/videos", response_model=DouyinFolderVideosResponse)
async def get_douyin_folder_videos(
    folder_id: int,
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """Get videos in a Douyin folder with is_processed status."""
    _, user_scope = await _require_douyin_session(session_id, db)
    folder = await _verify_folder_ownership(db, folder_id, user_scope)

    # 优化：单次 join 查询替代逐行 scalar 查询
    rows = (await db.execute(
        select(
            FavoriteVideo.bvid,
            FavoriteVideo.is_selected,
            VideoCache.title,
            VideoCache.owner_name,
            VideoCache.duration,
            VideoCache.is_processed,
        )
        .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid, isouter=True)
        .where(
            FavoriteVideo.folder_id == folder_id,
        )
    )).all()

    videos = []
    indexed_count = 0
    for bvid, is_selected, title, owner_name, duration, is_processed in rows:
        is_proc = bool(is_processed)
        if is_proc:
            indexed_count += 1
        videos.append({
            "video_id": bvid,
            "title": title if title else bvid,
            "author": owner_name or "",
            "duration": duration or 0,
            "is_processed": is_proc,
            "is_selected": bool(is_selected),
        })

    return {
        "folder_id": folder_id,
        "folder_title": folder.title or "",
        "videos": videos,
        "total": len(videos),
        "indexed_count": indexed_count,
    }


@router.post("/folders/{folder_id}/select")
async def toggle_folder_select(
    folder_id: int,
    select_all: bool = Query(True),
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """Toggle select all / deselect all videos in a folder."""
    _, user_scope = await _require_douyin_session(session_id, db)
    folder = await _verify_folder_ownership(db, folder_id, user_scope)

    links = (await db.execute(
        select(FavoriteVideo).where(FavoriteVideo.folder_id == folder.id)
    )).scalars().all()

    folder.is_selected = select_all
    for link in links:
        link.is_selected = select_all
    await db.commit()
    return {"success": True, "folder_id": folder_id, "selected": select_all}


@router.post("/folders/video/{video_id}/select")
async def toggle_video_select(
    video_id: str,
    folder_id: int = Query(..., description="文件夹ID"),
    selected: bool = Query(True),
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """Toggle select a single video in a folder."""
    _, user_scope = await _require_douyin_session(session_id, db)
    await _verify_folder_ownership(db, folder_id, user_scope)

    link = await db.scalar(
        select(FavoriteVideo).where(
            FavoriteVideo.folder_id == folder_id,
            FavoriteVideo.bvid == video_id,
        )
    )
    if not link:
        raise HTTPException(status_code=404, detail="视频不在文件夹中")

    link.is_selected = selected
    await db.commit()
    return {"success": True, "video_id": video_id, "selected": selected}


# ---------------------------------------------------------------------------
#  视频列表 & 单视频入库
# ---------------------------------------------------------------------------

@router.get("/videos", response_model=VideoListResponse)
async def list_douyin_videos(
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户已入库的抖音视频。"""
    _, user_scope = await _require_douyin_session(session_id, db)

    rows = (await db.execute(
        select(VideoCache)
        .where(VideoCache.platform == "douyin")
        .order_by(VideoCache.created_at.desc())
    )).scalars().all()

    # 按当前用户隔离：只返回与该用户 folder 关联的视频
    user_bvids = set()
    folder_rows = (await db.execute(
        select(FavoriteFolder.id).where(
            FavoriteFolder.platform == "douyin",
            FavoriteFolder.session_id.like(f"douyin-{user_scope}-%"),
        )
    )).all()
    folder_ids = [row[0] for row in folder_rows]
    if folder_ids:
        link_rows = (await db.execute(
            select(FavoriteVideo.bvid).where(FavoriteVideo.folder_id.in_(folder_ids))
        )).all()
        user_bvids = {row[0] for row in link_rows}

    videos: list[VideoItem] = []
    for cache in rows:
        if cache.bvid not in user_bvids:
            continue
        videos.append(VideoItem(
            video_id=cache.bvid,
            title=cache.title or cache.bvid,
            author=cache.owner_name or "",
            duration=cache.duration or 0,
            is_processed=bool(cache.is_processed),
            created_at=cache.created_at,
        ))
    return VideoListResponse(total=len(videos), videos=videos)


@router.post("/ingest", response_model=IngestResponse)
async def ingest_douyin_video(
    payload: IngestRequest,
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """手动入库单个抖音视频：写入 VideoCache 并加入 RAG 向量库。"""
    trace_ctx = TraceContext(step=f"douyin_ingest:{payload.video_id}")
    trace_ctx.__enter__()
    trace_logger.info(f"开始入库抖音视频: {payload.video_id}")
    try:
        return await _ingest_douyin_video_impl(
            payload, session_id, db,
        )
    finally:
        trace_ctx.__exit__(None, None, None)


async def _ingest_douyin_video_impl(
    payload, session_id, db,
):
    sid, user_scope = await _require_douyin_session(session_id, db)

    vid = (payload.video_id or "").strip()
    if not vid:
        raise HTTPException(status_code=400, detail="video_id 不能为空")

    title = payload.title or f"Douyin-{vid}"
    cache = None
    try:
        cache = await db.scalar(
            select(VideoCache).where(
                VideoCache.bvid == vid,
                VideoCache.platform == "douyin",
            )
        )
        if cache is None:
            cache = VideoCache(
                bvid=vid,
                platform="douyin",
                title=title,
                description=payload.description or "",
                owner_name=payload.author or "",
                duration=payload.duration or 0,
                pic_url=payload.cover_url or "",
                is_processed=False,
            )
            db.add(cache)
        else:
            cache.title = title or cache.title
            if payload.description:
                cache.description = payload.description
            if payload.author:
                cache.owner_name = payload.author
            if payload.duration:
                cache.duration = payload.duration
            if payload.cover_url:
                cache.pic_url = payload.cover_url

        rag = get_rag()
        douyin = DouyinPlatformService()
        try:
            content = None
            try:
                content = await douyin.fetch_content(vid, ASRService())
            except Exception as e:
                stage = classify_error(e)
                cache.last_error_stage = stage.value
                cache.last_error_detail = f"{type(e).__name__}: {str(e)}"
                cache.retry_count = (cache.retry_count or 0) + 1
                cache.permanent_failure = is_permanent(stage)
                trace_logger.warning(
                    f"[Douyin] ingest 抓取内容失败 [{vid}]: stage={stage.value} "
                    f"retry_count={cache.retry_count} permanent={cache.permanent_failure} error={e}"
                )
            if content:
                cache.content = content.content
                cache.content_source = content.source.value
                cache.outline_json = content.outline
                try:
                    await asyncio.to_thread(rag.delete_video, vid)
                except Exception as e:
                    trace_logger.warning(f"重试前清理旧向量失败 [{vid}]: {e}")
                chunks = await asyncio.to_thread(rag.add_video_content, content)
                if chunks > 0:
                    has_vectors = await asyncio.to_thread(rag.has_video, vid)
                    if has_vectors:
                        _clear_cache_error(cache)
                        trace_logger.info(f"[Douyin] 向量写入成功 [{vid}]")
                    else:
                        cache.is_processed = False
                        cache.process_error = "向量写入验证失败：向量库中未检测到向量"
                        _record_cache_error(cache, RuntimeError("向量写入验证失败"))
                        trace_logger.error(f"[Douyin] 向量验证失败 [{vid}]: 写入成功但向量库未检测到")
                else:
                    cache.is_processed = False
                    cache.process_error = "未生成可写入的向量文档"
                    _record_cache_error(cache, RuntimeError("未生成可写入的向量文档"))
            else:
                cache.is_processed = False
                cache.process_error = "无法获取视频内容"
                _record_cache_error(cache, RuntimeError("无法获取视频内容"))
        finally:
            await douyin.close()

        folder_title = "抖音手动入库"
        folder = await db.scalar(
            select(FavoriteFolder).where(
                FavoriteFolder.platform == "douyin",
                FavoriteFolder.session_id == f"douyin-{user_scope}-{folder_title}",
                FavoriteFolder.title == folder_title,
            )
        )
        if folder is None:
            folder = FavoriteFolder(
                session_id=f"douyin-{user_scope}-{folder_title}",
                platform="douyin",
                media_id=0,
                title=folder_title,
                media_count=0,
                is_selected=True,
            )
            db.add(folder)
            await db.flush()

        link = await db.scalar(
            select(FavoriteVideo).where(
                FavoriteVideo.folder_id == folder.id,
                FavoriteVideo.bvid == vid,
            )
        )
        if link is None:
            try:
                db.add(FavoriteVideo(folder_id=folder.id, bvid=vid, is_selected=True))
                await db.flush()
                folder.media_count = (folder.media_count or 0) + 1
            except IntegrityError:
                trace_logger.debug(f"FavoriteVideo 并发写入冲突 [{vid}]，已忽略")

        await db.commit()
        trace_logger.info(f"抖音视频入库完成: {vid}")
        return IngestResponse(
            video_id=vid,
            title=title,
            message="入库成功" if cache.is_processed else (cache.process_error or "入库失败"),
        )
    except HTTPException:
        raise
    except Exception as e:
        stage = classify_error(e)
        if cache:
            _record_cache_error(cache, e)
            should_retry = is_transient(stage) and (cache.retry_count <= 3) and not cache.permanent_failure
        else:
            should_retry = False
        trace_logger.error(
            f"[Douyin] ingest 失败 [{vid}]: stage={stage.value} "
            f"retry_count={cache.retry_count if cache else 0} permanent={cache.permanent_failure if cache else False} "
            f"should_retry={should_retry} error={e}"
        )
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"入库失败: {e}")


@router.post("/ingest/stream")
async def ingest_douyin_video_stream(
    payload: IngestRequest,
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """手动入库单个抖音视频（SSE 推送步骤进度）。

    使用 text/event-stream 推送事件，step 取值：
      scrape_page / download_video / extract_audio / asr / embedding / done / error。
    保留 POST /douyin/ingest 同步端点供旧客户端使用。
    """
    sid, user_scope = await _require_douyin_session(session_id, db)

    vid = (payload.video_id or "").strip()
    if not vid:
        raise HTTPException(status_code=400, detail="video_id 不能为空")

    title = payload.title or f"Douyin-{vid}"

    async def _event_stream():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _on_progress(step: str, status: str, message: str) -> None:
            try:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"step": step, "status": status, "message": message},
                )
            except Exception as cb_err:
                logger.debug(f"SSE progress_callback 异常: {cb_err}")

        async def _run():
            try:
                # 1. 写入或更新 VideoCache
                cache = await db.scalar(
                    select(VideoCache).where(
                        VideoCache.bvid == vid,
                        VideoCache.platform == "douyin",
                    )
                )
                if cache is None:
                    cache = VideoCache(
                        bvid=vid,
                        platform="douyin",
                        title=title,
                        description=payload.description or "",
                        owner_name=payload.author or "",
                        duration=payload.duration or 0,
                        pic_url=payload.cover_url or "",
                        is_processed=False,
                    )
                    db.add(cache)
                else:
                    cache.title = title or cache.title
                    if payload.description:
                        cache.description = payload.description
                    if payload.author:
                        cache.owner_name = payload.author
                    if payload.duration:
                        cache.duration = payload.duration
                    if payload.cover_url:
                        cache.pic_url = payload.cover_url

                # 2. 抓取内容 + ASR
                rag = get_rag()
                douyin = DouyinPlatformService()
                try:
                    content = None
                    try:
                        content = await douyin.fetch_content(
                            vid,
                            ASRService(),
                            progress_callback=_on_progress,
                        )
                    except Exception as e:
                        stage = classify_error(e)
                        cache.last_error_stage = stage.value
                        cache.last_error_detail = f"{type(e).__name__}: {str(e)}"
                        cache.retry_count = (cache.retry_count or 0) + 1
                        cache.permanent_failure = is_permanent(stage)
                        logger.warning(
                            f"[Douyin] SSE ingest 抓取内容失败 [{vid}]: stage={stage.value} "
                            f"retry_count={cache.retry_count} permanent={cache.permanent_failure} error={e}"
                        )
                    if content:
                        cache.content = content.content
                        cache.content_source = content.source.value
                        cache.outline_json = content.outline
                        try:
                            await asyncio.to_thread(rag.delete_video, vid)
                        except Exception as e:
                            logger.warning(f"重试前清理旧向量失败 [{vid}]: {e}")
                        # 推送 embedding 步骤（fetch_content 不会发送该事件）
                        await queue.put({
                            "step": "embedding",
                            "status": "running",
                            "message": "正在写入向量库...",
                        })
                        chunks = await asyncio.to_thread(rag.add_video_content, content)
                        if chunks > 0:
                            _clear_cache_error(cache)
                        else:
                            cache.is_processed = False
                            cache.process_error = "未生成可写入的向量文档"
                            _record_cache_error(cache, RuntimeError("未生成可写入的向量文档"))
                    else:
                        cache.is_processed = False
                        cache.process_error = "无法获取视频内容"
                        _record_cache_error(cache, RuntimeError("无法获取视频内容"))
                finally:
                    await douyin.close()

                # 3. 写入收藏夹关联
                folder_title = "抖音手动入库"
                folder = await db.scalar(
                    select(FavoriteFolder).where(
                        FavoriteFolder.platform == "douyin",
                        FavoriteFolder.session_id == f"douyin-{user_scope}-{folder_title}",
                        FavoriteFolder.title == folder_title,
                    )
                )
                if folder is None:
                    folder = FavoriteFolder(
                        session_id=f"douyin-{user_scope}-{folder_title}",
                        platform="douyin",
                        media_id=0,
                        title=folder_title,
                        media_count=0,
                        is_selected=True,
                    )
                    db.add(folder)
                    await db.flush()

                link = await db.scalar(
                    select(FavoriteVideo).where(
                        FavoriteVideo.folder_id == folder.id,
                        FavoriteVideo.bvid == vid,
                    )
                )
                if link is None:
                    try:
                        db.add(FavoriteVideo(folder_id=folder.id, bvid=vid, is_selected=True))
                        await db.flush()
                        folder.media_count = (folder.media_count or 0) + 1
                    except Exception as ie:
                        if "uq_folder_bvid" in str(ie) or "UNIQUE constraint" in str(ie).lower():
                            logger.debug(f"FavoriteVideo 并发写入冲突 [{vid}]，已忽略")
                        else:
                            raise

                await db.commit()
                await queue.put({
                    "step": "done",
                    "status": "completed",
                    "message": "入库成功" if cache.is_processed else (cache.process_error or "入库失败"),
                })
            except HTTPException as he:
                await queue.put({
                    "step": "error",
                    "status": "failed",
                    "message": str(he.detail),
                })
                try:
                    await db.rollback()
                except Exception:
                    pass
            except Exception as e:
                stage = classify_error(e)
                _record_cache_error(cache, e)
                logger.error(
                    f"[Douyin] SSE ingest 失败 [{vid}]: stage={stage.value} "
                    f"retry_count={cache.retry_count} permanent={cache.permanent_failure} error={e}"
                )
                await queue.put({
                    "step": "error",
                    "status": "failed",
                    "message": f"入库失败: {e}",
                })
                try:
                    await db.rollback()
                except Exception:
                    pass
            finally:
                await queue.put(None)

        task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                event_name = "done" if item.get("step") == "done" else (
                    "error" if item.get("step") == "error" else "progress"
                )
                yield (
                    f"event: {event_name}\n"
                    f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                )
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except Exception:
                    pass

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
#  批量入库：将已同步但未处理（is_processed=False）的视频批量写入知识库
# ---------------------------------------------------------------------------

class BatchIngestRequest(BaseModel):
    """批量入库请求：支持传递用户选择的视频ID列表。"""
    folder_id: Optional[int] = None
    video_ids: Optional[List[str]] = None
    limit: int = 20
    batch_size: int = 20


class BatchIngestResponse(BaseModel):
    total_pending: int
    processed: int
    succeeded: int
    failed: int
    results: list[dict]


@router.post("/ingest-batch")
async def batch_ingest_douyin_videos(
    payload: BatchIngestRequest,
    background_tasks: BackgroundTasks,
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """批量入库：遍历当前用户未处理的抖音视频，逐个抓取内容 + ASR + 写入向量库。

    支持两种模式：
    - 指定 video_ids：只处理指定视频，不分页限制
    - 未指定 video_ids：基于文件夹 + limit 限制的原有逻辑
    """
    sid, user_scope = await _require_douyin_session(session_id, db)

    if payload.video_ids:
        video_ids = list(dict.fromkeys(payload.video_ids))
        return await _start_batch_ingest_by_video_ids(
            sid, user_scope, video_ids, payload.batch_size, background_tasks, db,
        )

    folder_query = select(FavoriteFolder.id).where(
        FavoriteFolder.platform == "douyin",
        FavoriteFolder.session_id.like(f"douyin-{user_scope}-%"),
    )
    if payload.folder_id:
        folder_query = folder_query.where(FavoriteFolder.id == payload.folder_id)
    folder_rows = (await db.execute(folder_query)).all()
    folder_ids = [row[0] for row in folder_rows]
    if not folder_ids:
        return {
            "task_id": None,
            "total_pending": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "message": "没有待入库的视频",
        }

    link_rows = (await db.execute(
        select(FavoriteVideo.bvid, FavoriteVideo.folder_id)
        .where(FavoriteVideo.folder_id.in_(folder_ids))
    )).all()
    candidate_bvids = list({row[0] for row in link_rows})
    if not candidate_bvids:
        return {
            "task_id": None,
            "total_pending": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "message": "没有待入库的视频",
        }

    cache_rows = (await db.execute(
        select(VideoCache).where(
            VideoCache.bvid.in_(candidate_bvids),
            VideoCache.platform == "douyin",
            VideoCache.is_processed.is_(False),
        ).limit(payload.limit)
    )).scalars().all()

    total_pending = len(candidate_bvids)
    to_process_meta: list[dict] = [
        {
            "bvid": cache.bvid,
            "title": cache.title or cache.bvid,
            "description": cache.description or "",
            "owner_name": cache.owner_name or "",
            "duration": cache.duration or 0,
            "pic_url": cache.pic_url or "",
        }
        for cache in cache_rows
    ]

    # 去重键：同一 user_scope 同时只能有一个 batch_ingest_douyin 任务
    dedup_video_id = f"douyin_batch:{user_scope}"
    metadata = {
        "platform": "douyin",
        "session_id": sid,
        "total_videos": len(to_process_meta),
        "processed_videos": 0,
        "total_folders": None,
        "processed_folders": None,
        "current_folder_id": None,
        "current_folder_title": None,
        "current_video_title": None,
        "message": "",
        "succeeded": 0,
        "failed": 0,
        "total_pending": total_pending,
    }
    task_id = await task_tracker.create_task_if_not_exists(
        task_type="batch_ingest_douyin",
        video_id=dedup_video_id,
        metadata=metadata,
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有抖音批量入库任务正在运行，请等待完成后再试")
    await task_tracker.update_task(task_id, step="开始批量入库...")

    background_tasks.add_task(
        _ingest_douyin_batch_task,
        task_id,
        sid,
        user_scope,
        to_process_meta,
        payload.batch_size,
    )

    return {
        "task_id": task_id,
        "total_pending": total_pending,
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "results": [],
        "message": "批量入库任务已启动",
    }


async def _start_batch_ingest_by_video_ids(
    sid: str,
    user_scope: str,
    video_ids: List[str],
    batch_size: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
) -> dict:
    """按视频ID启动批量入库任务的公共逻辑。"""
    trace_ctx = TraceContext(step=f"douyin_batch:{len(video_ids)}")
    trace_ctx.__enter__()
    trace_logger.info(f"开始抖音批量入库任务: 视频数: {len(video_ids)}")
    try:
        return await _ingest_douyin_batch_impl(
            sid, user_scope, video_ids, batch_size, background_tasks, db,
        )
    finally:
        trace_ctx.__exit__(None, None, None)


async def _ingest_douyin_batch_impl(
    sid: str,
    user_scope: str,
    video_ids: List[str],
    batch_size: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
) -> dict:
    cache_rows = (await db.execute(
    select(VideoCache).where(
        VideoCache.bvid.in_(video_ids),
        VideoCache.platform == "douyin",
    )
)).scalars().all()

    to_process_meta: list[dict] = []
    for vid in video_ids:
        cache = next((c for c in cache_rows if c.bvid == vid), None)
        if cache:
            to_process_meta.append({
                "bvid": cache.bvid,
                "title": cache.title or cache.bvid,
                "description": cache.description or "",
                "owner_name": cache.owner_name or "",
                "duration": cache.duration or 0,
                "pic_url": cache.pic_url or "",
            })
        else:
            to_process_meta.append({
                "bvid": vid,
                "title": vid,
                "description": "",
                "owner_name": "",
                "duration": 0,
                "pic_url": "",
            })

    if not to_process_meta:
        return {
            "task_id": None,
            "total_pending": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "message": "没有待入库的视频",
        }

    # 去重键：同一 user_scope 同时只能有一个 batch_ingest_douyin 任务
    dedup_video_id = f"douyin_batch:{user_scope}"
    metadata = {
        "platform": "douyin",
        "session_id": sid,
        "total_videos": len(to_process_meta),
        "processed_videos": 0,
        "total_folders": None,
        "processed_folders": None,
        "current_folder_id": None,
        "current_folder_title": None,
        "current_video_title": None,
        "message": "",
        "succeeded": 0,
        "failed": 0,
        "total_pending": len(to_process_meta),
    }
    task_id = await task_tracker.create_task_if_not_exists(
        task_type="batch_ingest_douyin",
        video_id=dedup_video_id,
        metadata=metadata,
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有抖音批量入库任务正在运行，请等待完成后再试")
    await task_tracker.update_task(task_id, step="开始批量入库...")

    background_tasks.add_task(
        _ingest_douyin_batch_task,
        task_id,
        sid,
        user_scope,
        to_process_meta,
        batch_size,
    )

    return {
        "task_id": task_id,
        "total_pending": len(to_process_meta),
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "results": [],
        "message": "批量入库任务已启动",
    }


async def _ingest_douyin_batch_task(
    task_id: str,
    session_id: str,
    user_scope: str,
    to_process_meta: list[dict],
    batch_size: int = 20,
):
    """抖音批量入库后台任务：支持分批处理 + 批次内并发。

    使用 asyncio.Semaphore 控制批次内并发数，通过锁保护共享状态。
    """
    total = len(to_process_meta)
    succeeded = 0
    failed = 0
    results: list[dict] = []
    state_lock = asyncio.Lock()

    try:
        from app.config import settings
        max_concurrent = settings.max_concurrent_ingestion
    except Exception:
        max_concurrent = 5

    async def _report_progress(step: Optional[str] = None) -> None:
        processed = succeeded + failed
        update_kwargs: dict = {
            "progress": int(processed / total * 100) if total else 100,
            "metadata": {
                "processed_videos": processed,
                "succeeded": succeeded,
                "failed": failed,
            },
        }
        if step is not None:
            update_kwargs["step"] = step
        await task_tracker.update_task(task_id, **update_kwargs)

    try:
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.RUNNING,
            step="开始批量入库...",
        )

        batches = [
            to_process_meta[i:i + batch_size]
            for i in range(0, total, batch_size)
        ]
        total_batches = len(batches)

        rag = get_rag()
        douyin_service = DouyinPlatformService()

        semaphore = asyncio.Semaphore(max_concurrent)

        async def _process_single_video(meta: dict):
            nonlocal succeeded, failed
            vid = meta["bvid"]

            async with semaphore:
                await task_tracker.update_task(
                    task_id,
                    metadata={"current_video_title": meta.get("title") or vid},
                )
                try:
                    async with get_db_context() as db:
                        cache = await db.scalar(
                            select(VideoCache).where(
                                VideoCache.bvid == vid,
                                VideoCache.platform == "douyin",
                            )
                        )
                        if not cache:
                            async with state_lock:
                                failed += 1
                                results.append({
                                    "video_id": vid,
                                    "title": meta.get("title") or vid,
                                    "status": "fail",
                                    "error": "视频缓存不存在",
                                })
                            await db.commit()
                            return

                        content = None
                        try:
                            content = await douyin_service.fetch_content(vid, ASRService())
                        except Exception as e:
                            stage = classify_error(e)
                            cache.last_error_stage = stage.value
                            cache.last_error_detail = f"{type(e).__name__}: {str(e)}"
                            cache.retry_count = (cache.retry_count or 0) + 1
                            cache.permanent_failure = is_permanent(stage)
                            logger.warning(
                                f"[Douyin] batch ingest 抓取内容失败 [{vid}]: stage={stage.value} "
                                f"retry_count={cache.retry_count} permanent={cache.permanent_failure} error={e}"
                            )

                        if content:
                            cache.content = content.content
                            cache.content_source = content.source.value
                            cache.outline_json = content.outline
                            try:
                                await asyncio.to_thread(rag.delete_video, vid)
                            except Exception as e:
                                logger.warning(f"重试前清理旧向量失败 [{vid}]: {e}")
                            chunks = await asyncio.to_thread(rag.add_video_content, content)
                            if chunks > 0:
                                _clear_cache_error(cache)
                                async with state_lock:
                                    succeeded += 1
                                    results.append({
                                        "video_id": vid,
                                        "title": cache.title or vid,
                                        "status": "ok",
                                    })
                            else:
                                cache.is_processed = False
                                cache.process_error = "未生成可写入的向量文档"
                                _record_cache_error(cache, RuntimeError("未生成可写入的向量文档"))
                                async with state_lock:
                                    failed += 1
                                    results.append({
                                        "video_id": vid,
                                        "title": cache.title or vid,
                                        "status": "fail",
                                        "error": "未生成向量文档",
                                    })
                        else:
                            cache.is_processed = False
                            cache.process_error = "无法获取视频内容"
                            _record_cache_error(cache, RuntimeError("无法获取视频内容"))
                            async with state_lock:
                                failed += 1
                                results.append({
                                    "video_id": vid,
                                    "title": cache.title or vid,
                                    "status": "fail",
                                    "error": "无法获取内容",
                                })
                        await db.commit()
                except Exception as e:
                    stage = classify_error(e)
                    async with get_db_context() as db:
                        cache = await db.scalar(
                            select(VideoCache).where(
                                VideoCache.bvid == vid,
                                VideoCache.platform == "douyin",
                            )
                        )
                        _record_cache_error(cache, e)
                        await db.commit()
                    async with state_lock:
                        failed += 1
                        results.append({
                            "video_id": vid,
                            "title": meta.get("title") or vid,
                            "status": "fail",
                            "error": str(e),
                        })
                    logger.error(
                        f"[Douyin] batch ingest 单视频失败 [{vid}]: stage={stage.value} error={e}"
                    )

                async with state_lock:
                    # succeeded/failed 已在锁内更新，此处仅用于触发进度上报
                    pass

                await _report_progress()

        try:
            for batch_idx, batch in enumerate(batches):
                await task_tracker.update_task(
                    task_id,
                    step=f"批次 {batch_idx + 1}/{total_batches}：处理 {len(batch)} 个视频",
                )
                batch_tasks = [_process_single_video(meta) for meta in batch]
                await asyncio.gather(*batch_tasks, return_exceptions=True)
                await task_tracker.update_task(
                    task_id,
                    step=f"批次 {batch_idx + 1}/{total_batches} 完成",
                )
        finally:
            await douyin_service.close()

        # 保留原语义：全部失败则 FAILED，否则 SUCCESS（部分失败在 message 中体现）
        final_status = TaskStatus.FAILED if (failed and not succeeded) else TaskStatus.SUCCESS
        if failed and not succeeded:
            final_message = f"批量入库失败：{failed} 个视频处理失败"
        elif failed:
            final_message = f"入库完成：成功 {succeeded}，失败 {failed}"
        else:
            final_message = f"入库完成：成功 {succeeded}"
        await task_tracker.update_task(
            task_id,
            status=final_status,
            progress=100,
            step="失败" if failed and not succeeded else "完成",
            metadata={
                "current_video_title": None,
                "message": final_message,
                "results": results,
            },
        )
        trace_logger.info(f"[Douyin] 批量入库结束: 成功 {succeeded}，失败 {failed}")

    except Exception as e:
        trace_logger.error(f"[Douyin] 批量入库任务崩溃: {e}", exc_info=True)
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error_message=f"批量入库任务异常: {e}",
            metadata={"message": f"批量入库任务异常: {e}"},
        )


@router.post("/ingest-by-ids")
async def ingest_by_video_ids(
    payload: BatchIngestRequest,
    background_tasks: BackgroundTasks,
    session_id: Optional[str] = Query(None, description="可选会话ID，传入则严格校验"),
    db: AsyncSession = Depends(get_db),
):
    """按视频ID列表直接入库。

    与 /ingest-batch 的区别：此端点专注于"用户选择视频"场景，
    必须通过 video_ids 传入待入库视频；不传则返回 400。
    当视频数量超过 batch_size 时自动分批处理。
    """
    if not payload.video_ids:
        raise HTTPException(status_code=400, detail="必须提供 video_ids 列表")

    sid, user_scope = await _require_douyin_session(session_id, db)
    video_ids = list(dict.fromkeys(payload.video_ids))

    return await _start_batch_ingest_by_video_ids(
        sid, user_scope, video_ids, payload.batch_size, background_tasks, db,
    )


@router.get("/ingest-batch/{task_id}/status", response_model=BuildStatus, deprecated=True)
async def get_douyin_ingest_batch_status(task_id: str):
    """查询抖音批量入库任务状态 (deprecated: 请改用 GET /api/tasks/{task_id})

    通过 task_tracker_service 查询任务状态，返回 BuildStatus 模型。
    """
    task = await task_tracker.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task_info_to_build_status(task_id, task)
