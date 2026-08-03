"""
Bilibili RAG 知识库系统

知识库路由 - 构建和管理知识库
"""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Depends, Response, UploadFile, File
from fastapi.responses import StreamingResponse
from loguru import logger
from typing import List, Optional, Callable, Literal
from pydantic import BaseModel
from sqlalchemy import select, func, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

import aiofiles

from app.database import get_db, get_db_context
from app.models import FavoriteFolder, FavoriteVideo, VideoCache, UserSession, ContentSource, VideoContent, utcnow
from app.services.bilibili import BilibiliService
from app.services.content_fetcher import ContentFetcher
from app.services.asr import ASRService
from app.services.rag import RAGService
from app.services.markdown_export import build_video_markdown, organize_video_content
from app.services.cancellation import CancelCheck, OperationCancelled, ensure_not_cancelled
from app.services.error_classifier import classify_error, is_transient, is_permanent, ErrorStage
from app.services.task_tracker import (
    build_tasks,
    BuildStatus,
    prune_expired_build_tasks as _prune_expired_build_tasks,
    task_info_to_build_status,
)
from app.services.task_tracker_service import task_tracker, TaskStatus
from app.routers.auth import get_session
from app.services.tracing import TraceContext, trace_logger, set_operation_type

router = APIRouter(prefix="/knowledge", tags=["知识库"])

# 全局 RAG 服务实例
_rag_services: dict[str, RAGService] = {}

# 单视频导出/入库操作取消状态
active_operations: dict[str, tuple[str, asyncio.Event]] = {}

# 本地文件上传入库配置
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mkv", ".flac", ".aac", ".ogg"}
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
CHUNK_SIZE = 1024 * 1024  # 1MB


def get_rag_service(platform: str = "bilibili") -> RAGService:
    """Get per-platform RAG service with isolated ChromaDB collection."""
    global _rag_services
    if platform not in _rag_services:
        collection_map = {
            "bilibili": "bilibili_videos",
            "douyin": "douyin_videos",
            "all": "bilibili_videos",
        }
        name = collection_map.get(platform, "bilibili_videos")
        _rag_services[platform] = RAGService(collection_name=name)
        logger.info(f"RAGService initialized (platform={platform}, collection={name})")
    return _rag_services[platform]


class BuildRequest(BaseModel):
    """知识库构建请求"""
    folder_ids: List[int]  # 要处理的收藏夹 ID 列表
    exclude_bvids: Optional[List[str]] = None  # 排除的视频


class FolderStatus(BaseModel):
    """收藏夹入库状态"""
    media_id: int
    indexed_count: int
    failed_count: int = 0
    media_count: Optional[int] = None
    last_sync_at: Optional[datetime] = None


class SyncRequest(BaseModel):
    """同步请求"""
    folder_ids: Optional[List[int]] = None


class SyncResult(BaseModel):
    """同步结果"""
    folder_id: int
    total: int
    added: int
    removed: int
    indexed: int
    failed: int = 0
    message: str
    last_sync_at: Optional[datetime] = None


class MarkdownExportRequest(BaseModel):
    """视频 Markdown 导出请求"""
    mode: Literal["original", "ai"] = "original"
    operation_id: Optional[str] = None


class SingleVideoIngestRequest(BaseModel):
    """单视频入库请求"""
    folder_id: int
    operation_id: Optional[str] = None


class VideoIngestItem(BaseModel):
    """单个视频入库项（跨平台）"""
    bvid: str
    platform: str  # "bilibili" | "douyin"
    tags: Optional[List[str]] = None  # 标签列表


class VideoIngestRequest(BaseModel):
    """视频级批量入库请求"""
    videos: List[VideoIngestItem]


class VideoListItem(BaseModel):
    """视频列表项（跨平台，含入库状态）"""
    bvid: str
    platform: str
    title: str
    author: str
    duration: int
    is_processed: bool
    process_error: Optional[str] = None
    folder_id: int
    folder_title: str
    tags: Optional[List[str]] = None  # 标签列表


def _parse_tags(raw: Optional[str]) -> Optional[List[str]]:
    """从 VideoCache.tags 的 JSON 字符串解析出标签列表。"""
    if not raw:
        return None
    try:
        tags = json.loads(raw)
        return tags if isinstance(tags, list) else None
    except (ValueError, TypeError):
        return None


def _start_operation(operation_id: Optional[str], session_id: str) -> Optional[asyncio.Event]:
    if not operation_id:
        return None
    existing = active_operations.get(operation_id)
    if existing:
        raise HTTPException(status_code=409, detail="操作标识已被占用")
    event = asyncio.Event()
    active_operations[operation_id] = (session_id, event)
    return event


def _finish_operation(operation_id: Optional[str], session_id: str) -> None:
    if not operation_id:
        return
    existing = active_operations.get(operation_id)
    if existing and existing[0] == session_id:
        active_operations.pop(operation_id, None)


async def _get_session_ids_for_user(db: AsyncSession, session_id: str) -> List[str]:
    """获取当前 Session 及同一 B 站账号的历史 Session（仅 bilibili 平台）。"""
    mid = await db.scalar(
        select(UserSession.bili_mid).where(
            UserSession.session_id == session_id,
            UserSession.platform == "bilibili",
        )
    )
    if not mid:
        return [session_id]
    rows = await db.execute(
        select(UserSession.session_id).where(
            UserSession.bili_mid == mid,
            UserSession.platform == "bilibili",
        )
    )
    session_ids = [row[0] for row in rows.fetchall()]
    return session_ids or [session_id]


async def _get_or_create_folder(
    db: AsyncSession,
    session_id: str,
    media_id: int,
    title: Optional[str] = None,
    media_count: Optional[int] = None,
) -> FavoriteFolder:
    """获取或创建收藏夹记录"""
    result = await db.execute(
        select(FavoriteFolder).where(
            FavoriteFolder.session_id == session_id,
            FavoriteFolder.media_id == media_id,
        )
    )
    folder = result.scalar_one_or_none()

    if folder is None:
        folder = FavoriteFolder(
            session_id=session_id,
            media_id=media_id,
            title=title or "",
            media_count=media_count or 0,
            is_selected=True,
        )
        db.add(folder)
        await db.flush()
    else:
        if title:
            folder.title = title
        if media_count is not None:
            folder.media_count = media_count

    return folder


def _extract_video_info(media: dict) -> tuple[str, str, Optional[int]]:
    """抽取视频关键信息"""
    bvid = media.get("bvid") or media.get("bv_id")
    title = media.get("title", bvid)
    cid = None
    ugc = media.get("ugc") or {}
    if ugc.get("first_cid"):
        cid = ugc.get("first_cid")
    else:
        cid = media.get("cid") or media.get("id")
    return bvid, title, cid


async def _upsert_video_cache(db: AsyncSession, bvid: str, meta: dict, platform: str = "bilibili") -> None:
    """写入或更新视频缓存信息

    Args:
        platform: 视频平台（bilibili / douyin）。显式传入避免依赖 DB default，
                  因为旧版本创建的 VideoCache 可能 platform=NULL，
                  会导致 /videos/list 的 WHERE platform='bilibili' 过滤掉这些视频。
    """
    result = await db.execute(select(VideoCache).where(VideoCache.bvid == bvid))
    cache = result.scalar_one_or_none()

    if cache is None:
        cache = VideoCache(
            bvid=bvid,
            cid=meta.get("cid"),
            title=meta.get("title") or bvid,
            description=meta.get("intro"),
            owner_name=meta.get("owner_name"),
            owner_mid=meta.get("owner_mid"),
            duration=meta.get("duration"),
            pic_url=meta.get("cover"),
            platform=platform,
            is_processed=False,
        )
        db.add(cache)
        return

    cache.title = meta.get("title") or cache.title
    if meta.get("cid") is not None:
        cache.cid = meta.get("cid")
    if meta.get("intro") is not None:
        cache.description = meta.get("intro")
    if meta.get("owner_name") is not None:
        cache.owner_name = meta.get("owner_name")
    if meta.get("owner_mid") is not None:
        cache.owner_mid = meta.get("owner_mid")
    if meta.get("duration") is not None:
        cache.duration = meta.get("duration")
    if meta.get("cover") is not None:
        cache.pic_url = meta.get("cover")
    # 修复旧数据：如果已存在记录的 platform 为空，补上当前平台
    if not cache.platform:
        cache.platform = platform


async def _ingest_single_video(
    db: AsyncSession,
    bili: BilibiliService,
    rag: RAGService,
    content_fetcher: ContentFetcher,
    session_id: str,
    folder_id: int,
    bvid: str,
    cancel_check: CancelCheck = None,
    progress_callback: Optional[Callable[[str, str, str], None]] = None,
    force_refresh: bool = False,
) -> VideoCache:
    """验证收藏关系并将单个视频写入缓存与向量库。

    Args:
        progress_callback: 可选的进度回调 (step, status, message) -> None。
            step 取值：fetch_info / download_audio / asr / embedding / done。
            status 取值：running / completed / failed。
    """
    def _emit(step: str, status: str, message: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(step, status, message)
            except Exception as cb_err:
                logger.debug(f"progress_callback 异常被忽略: {cb_err}")

    trace_ctx = TraceContext(step=f"ingest_video:{bvid}")
    trace_ctx.__enter__()
    trace_logger.info(f"开始入库视频: {bvid}")
    try:
        ensure_not_cancelled(cancel_check)
        _emit("fetch_info", "running", "正在获取视频信息...")
        info_result = await bili.get_favorite_content(folder_id, pn=1, ps=1)
        ensure_not_cancelled(cancel_check)
        folder_info = info_result.get("info", {})
        videos = await bili.get_all_favorite_videos(folder_id)
        ensure_not_cancelled(cancel_check)
        media = next((item for item in videos if (item.get("bvid") or item.get("bv_id")) == bvid), None)
        if media is None:
            raise HTTPException(status_code=404, detail="该视频不在指定收藏夹中")

        title = media.get("title", bvid)
        if media.get("attr", 0) == 9 or title in ["已失效视频", "已删除视频"]:
            raise HTTPException(status_code=409, detail="视频已失效，无法入库")

        _, title, cid = _extract_video_info(media)
        owner = media.get("upper") or {}
        meta = {
            "title": title,
            "cid": cid,
            "intro": media.get("intro"),
            "cover": media.get("cover"),
            "duration": media.get("duration"),
            "owner_name": owner.get("name"),
            "owner_mid": owner.get("mid"),
        }
        folder = await _get_or_create_folder(
            db,
            session_id=session_id,
            media_id=folder_id,
            title=folder_info.get("title"),
            media_count=folder_info.get("media_count", len(videos)),
        )
        ensure_not_cancelled(cancel_check)
        await _upsert_video_cache(db, bvid, meta)
        cache = await db.scalar(select(VideoCache).where(VideoCache.bvid == bvid))
        if cache is None:
            raise RuntimeError("写入视频缓存失败")

        relation = await db.scalar(
            select(FavoriteVideo.id).where(
                FavoriteVideo.folder_id == folder.id,
                FavoriteVideo.bvid == bvid,
            )
        )
        if relation is None:
            try:
                db.add(FavoriteVideo(folder_id=folder.id, bvid=bvid, is_selected=True))
                await db.flush()
            except IntegrityError:
                logger.debug(f"FavoriteVideo 并发写入冲突 [{bvid}]，已忽略")

        old_vector_deleted = False
        try:
            ensure_not_cancelled(cancel_check)
            has_vectors = await asyncio.to_thread(rag.has_video, bvid)
            ensure_not_cancelled(cancel_check)
            if force_refresh or not (cache.content or "").strip() or not cache.is_processed or not has_vectors:
                _emit("download_audio", "running", "下载音频并转写中...")
                content = await content_fetcher.fetch_content(
                    bvid,
                    cid=meta["cid"],
                    title=meta["title"],
                    description=meta.get("intro"),
                    owner_name=meta.get("owner_name"),
                    owner_mid=meta.get("owner_mid"),
                    duration=meta.get("duration"),
                )
                _emit("asr", "completed", "转写完成，准备写入向量库...")
                ensure_not_cancelled(cancel_check)
                cache.content = content.content
                cache.content_source = content.source.value
                cache.outline_json = content.outline
                _set_cache_processing_result(cache, RuntimeError("向量化进行中"))
                if has_vectors:
                    prepared_docs = await asyncio.to_thread(rag.prepare_documents, content)
                    ensure_not_cancelled(cancel_check)
                    if not prepared_docs:
                        raise RuntimeError(
                            "内容预校验失败：未生成有效文档，保留原有向量不删除"
                        )
                    try:
                        await asyncio.to_thread(rag.delete_video, bvid)
                        old_vector_deleted = True
                    except Exception as e:
                        trace_logger.error(f"删除旧向量失败 [{bvid}]，跳过本次更新: {e}")
                        raise
                    ensure_not_cancelled(cancel_check)
                _emit("embedding", "running", "正在写入向量库...")
                chunks = await asyncio.to_thread(
                    rag.add_video_content,
                    content,
                    cancel_check,
                )
                ensure_not_cancelled(cancel_check)
                if chunks <= 0:
                    raise RuntimeError("未生成可写入的向量文档")
                _set_cache_processing_result(cache)

                verified = await asyncio.to_thread(rag.has_video, bvid)
                if not verified:
                    trace_logger.warning(
                        f"向量写入验证警告 [{bvid}]: has_video 返回 False，"
                        f"但 add_video_content 返回 {chunks} 个 chunks，已信任写入结果"
                    )
                else:
                    trace_logger.info(f"向量写入验证通过 [{bvid}]")

            folder.last_sync_at = utcnow()
            ensure_not_cancelled(cancel_check)
            await db.commit()
            _emit("done", "completed", "入库成功")
            trace_logger.info(f"视频入库完成: {bvid}")
            return cache
        except OperationCancelled:
            _emit("error", "failed", "操作已取消")
            trace_logger.warning(f"视频入库被取消: {bvid}")
            try:
                await asyncio.to_thread(rag.delete_video, bvid)
            except Exception as cleanup_err:
                trace_logger.warning(f"取消后清理向量失败 [{bvid}]: {cleanup_err}")
            await db.rollback()
            raise
        except Exception as e:
            _emit("error", "failed", f"入库失败: {e}")
            trace_logger.error(f"视频入库异常 [{bvid}]: {e}")
            try:
                await asyncio.to_thread(rag.delete_video, bvid)
            except Exception as cleanup_err:
                trace_logger.warning(f"异常后清理向量失败 [{bvid}]: {cleanup_err}")
            _record_cache_error(cache, e)
            if old_vector_deleted and cache:
                cache.process_error = f"向量写入失败，旧向量已删除: {e}"
            should_retry = is_transient(classify_error(e)) and (cache.retry_count <= 3) and not cache.permanent_failure
            if should_retry:
                trace_logger.info(
                    "视频 [{bvid}] 为临时性错误 (stage={stage})，将在下次同步时自动重试 "
                    "(retry_count={retry})",
                    bvid=bvid,
                    stage=cache.last_error_stage,
                    retry=cache.retry_count,
                )
            else:
                trace_logger.warning(
                    "视频 [{bvid}] 为永久性错误或已达重试上限 (retry_count={retry})，不再自动重试",
                    bvid=bvid,
                    retry=cache.retry_count,
                )
            await db.commit()
            raise
    finally:
        trace_ctx.__exit__(None, None, None)


def _set_cache_processing_result(cache: Optional[VideoCache], error: Optional[Exception] = None) -> None:
    """记录内容是否已成功写入向量库。"""
    if cache is None:
        return
    if error is None:
        # 入库成功：清除所有错误字段并重置重试计数
        cache.is_processed = True
        cache.process_error = None
        cache.last_error_stage = None
        cache.last_error_detail = None
        cache.permanent_failure = False
        cache.retry_count = 0
    else:
        cache.is_processed = False
        cache.process_error = str(error)


def _record_cache_error(cache: Optional[VideoCache], error: Exception) -> None:
    """记录视频处理错误详情到 VideoCache，包含错误分类和重试计数。"""
    if cache is None:
        return
    stage = classify_error(error)
    cache.is_processed = False
    cache.process_error = str(error)
    cache.last_error_stage = stage.value
    cache.last_error_detail = f"{type(error).__name__}: {str(error)}"
    cache.retry_count = (cache.retry_count or 0) + 1
    cache.permanent_failure = is_permanent(stage)
    logger.error(
        "视频处理失败: bvid={} stage={} retry_count={} permanent={} error={}",
        cache.bvid, stage.value, cache.retry_count, cache.permanent_failure, str(error),
    )


async def _sync_folder(
    db: AsyncSession,
    bili: BilibiliService,
    rag: RAGService,
    content_fetcher: ContentFetcher,
    session_id: str,
    folder_id: int,
    exclude_bvids: Optional[set[str]] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    force_refresh: bool = False,
) -> dict:
    """同步单个收藏夹到向量库

    核心步骤（对应修复点：先 prepare_documents 预校验再 rag.delete_video 删除旧向量）：
    1. 获取收藏夹视频列表，逐视频 upsert 到 VideoCache
    2. 对每个待入库视频：先调用 rag.prepare_documents(content) 预校验内容有效性，
       校验通过后才调用 rag.delete_video(bvid) 删旧向量，最后调用
       rag.add_video_content(content) 写入新向量，避免"先删后加"的丢失窗口
    """
    trace_ctx = TraceContext(step=f"sync_folder:{folder_id}")
    trace_ctx.__enter__()
    trace_logger.info(f"开始同步收藏夹: {folder_id}")
    try:
        # 读取增量同步游标：last_sync_at 非 None 时仅拉取 fav_time > last_sync_at 的新增视频
        from app.services.sync_cursor import get_cursor as _get_sync_cursor
        from app.services.sync_cursor import upsert_cursor as _upsert_sync_cursor
        cursor = await _get_sync_cursor(db, "bilibili", str(folder_id))
        last_sync_at = cursor.last_sync_at if cursor is not None else None
        incremental = last_sync_at is not None
        if incremental:
            trace_logger.info(f"[{folder_id}] 增量同步: last_sync_at={last_sync_at}")

        info = {}
        try:
            info_result = await bili.get_favorite_content(folder_id, pn=1, ps=1)
            info = info_result.get("info", {})
        except Exception as e:
            trace_logger.warning(f"获取收藏夹信息失败 [{folder_id}]: {e}")

        videos = await bili.get_all_favorite_videos(folder_id, last_sync_at=last_sync_at)
        total_in_folder = info.get("media_count", len(videos) if not incremental else 0)

        if not videos:
            # 增量同步下 "无新增视频" 是正常结果，不应抛错；仅全量同步的空列表才需中止
            if not incremental and total_in_folder and total_in_folder > 0:
                raise RuntimeError(
                    f"收藏夹 {folder_id} 返回空列表，已中止同步以避免误删"
                )

        video_map = {}
        skipped_invalid = 0
        # 记录本次拉取到的最大 fav_time，用于同步完成后更新游标
        max_fav_time: Optional[int] = last_sync_at
        for media in videos:
            bvid, title, cid = _extract_video_info(media)
            if not bvid:
                continue
            if exclude_bvids and bvid in exclude_bvids:
                continue

            attr = media.get("attr", 0)
            if attr == 9 or title in ["已失效视频", "已删除视频"]:
                skipped_invalid += 1
                logger.debug(f"跳过失效视频: {bvid} - {title}")
                continue

            owner = media.get("upper") or {}
            video_map[bvid] = {
                "title": title,
                "cid": cid,
                "intro": media.get("intro"),
                "cover": media.get("cover"),
                "duration": media.get("duration"),
                "owner_name": owner.get("name"),
                "owner_mid": owner.get("mid"),
            }

            # 更新最大 fav_time（仅取比当前游标更大的值，避免被异常小值拉低）
            fav_time = media.get("fav_time")
            if fav_time is not None:
                try:
                    ft = int(fav_time)
                    if max_fav_time is None or ft > max_fav_time:
                        max_fav_time = ft
                except (ValueError, TypeError):
                    pass

        if skipped_invalid > 0:
            trace_logger.info(f"[{folder_id}] 过滤了 {skipped_invalid} 个失效视频")

        valid_count = len(video_map)
        current_bvids = set(video_map.keys())

        _db_lock = asyncio.Lock()

        # 增量同步下 valid_count 只是新增数，不应覆盖收藏夹总数；
        # 优先用 B站 info.media_count（真实总数），缺失时保留已有值
        if incremental:
            media_count_for_folder = total_in_folder if total_in_folder else None
        else:
            media_count_for_folder = valid_count

        folder = await _get_or_create_folder(
            db,
            session_id=session_id,
            media_id=folder_id,
            title=info.get("title"),
            media_count=media_count_for_folder,
        )

        existing_rows = await db.execute(
            select(FavoriteVideo.bvid).where(FavoriteVideo.folder_id == folder.id)
        )
        existing_bvids = {row[0] for row in existing_rows.fetchall()}

        # 全量回退：增量同步下，如果本地无任何已收藏视频记录（existing_bvids 为空）
        # 但收藏夹之前已同步过（last_sync_at 非空），说明记录被外部删除（如用户在 RAG 管理面板删除视频）。
        # 此时增量同步无法发现这些视频（fav_time <= last_sync_at），需要回退到全量同步。
        if incremental and not existing_bvids and last_sync_at is not None:
            trace_logger.info(f"[{folder_id}] 检测到本地记录为空但游标存在，回退到全量同步")
            # 重新全量拉取所有视频
            videos = await bili.get_all_favorite_videos(folder_id, last_sync_at=None)
            total_in_folder = info.get("media_count", len(videos))
            video_map = {}
            max_fav_time = None
            for media in videos:
                bvid, title, cid = _extract_video_info(media)
                if not bvid:
                    continue
                if exclude_bvids and bvid in exclude_bvids:
                    continue
                attr = media.get("attr", 0)
                if attr == 9 or title in ["已失效视频", "已删除视频"]:
                    continue
                owner = media.get("upper") or {}
                video_map[bvid] = {
                    "title": title,
                    "cid": cid,
                    "intro": media.get("intro"),
                    "cover": media.get("cover"),
                    "duration": media.get("duration"),
                    "owner_name": owner.get("name"),
                    "owner_mid": owner.get("mid"),
                }
                fav_time = media.get("fav_time")
                if fav_time is not None:
                    try:
                        ft = int(fav_time)
                        if max_fav_time is None or ft > max_fav_time:
                            max_fav_time = ft
                    except (ValueError, TypeError):
                        pass
            current_bvids = set(video_map.keys())
            # 全量回退后不再是增量模式
            incremental = False
            trace_logger.info(f"[{folder_id}] 全量回退完成: 拉取到 {len(current_bvids)} 个视频")

        added = current_bvids - existing_bvids
        if incremental:
            # 增量同步只拉了新增视频，无法判断哪些被取消收藏，跳过 removed 检测
            removed: set[str] = set()
        else:
            removed = existing_bvids - current_bvids

        for bvid, meta in video_map.items():
            await _upsert_video_cache(db, bvid, meta)

        source_priority = {
            ContentSource.BASIC_INFO.value: 1,
            ContentSource.AI_SUMMARY.value: 2,
            ContentSource.SUBTITLE.value: 3,
            ContentSource.ASR.value: 4,
        }

        def _is_better_source(new_source: str, old_source: Optional[str]) -> bool:
            return source_priority.get(new_source, 0) > source_priority.get(old_source or "", 0)

        def _should_refresh_cache(cache: Optional[VideoCache], force_refresh: bool = False) -> bool:
            if force_refresh:
                return True
            if not cache:
                return True
            text = (cache.content or "").strip()
            if len(text) < 50:
                return True
            if cache.content_source in (None, "", ContentSource.BASIC_INFO.value):
                return True
            return False

        def _is_asr_cache_usable(cache: Optional[VideoCache]) -> bool:
            if not cache:
                return False
            if cache.content_source != ContentSource.ASR.value:
                return False
            text = (cache.content or "").strip()
            return len(text) >= 50

        update_candidates: set[str] = set()
        vector_presence: dict[str, bool] = {}
        # 预加载所有当前视频的向量存在状态（含新增视频），
        # 避免 _process_video Phase 1 在 _db_lock 内调用 rag.has_video（ChromaDB 查询）阻塞并发
        check_bvids = list(current_bvids)
        if check_bvids:
            vector_presence = await asyncio.to_thread(rag.has_videos, check_bvids)
        for bvid in current_bvids & existing_bvids:
            if bvid in added:
                continue
            result = await db.execute(select(VideoCache).where(VideoCache.bvid == bvid))
            cache = result.scalar_one_or_none()
            has_vectors = vector_presence.get(bvid, False)
            if cache and cache.is_processed and not has_vectors:
                # 向量缺失属于预检查（非处理失败），不累加 retry_count，
                # 仅标记为待重新入库，等待下次同步时重新向量化。
                cache.is_processed = False
                cache.process_error = "向量数据缺失，等待重新入库"
            if _should_refresh_cache(cache, force_refresh) or cache is None or not cache.is_processed or not has_vectors:
                update_candidates.add(bvid)

        # 增量同步下，current_bvids 只含新增视频，无法覆盖已存在但需要重新入库的视频。
        # 典型场景：用户在 RAG 管理面板"出库"后，is_processed=False 但 FavoriteVideo 仍在，
        # 增量同步不会从 B站重新拉取该视频，导致 update_candidates 为空、入库=0。
        # 修复：查询本文件夹下所有 is_processed=False 的视频，加入待处理队列。
        if incremental and existing_bvids:
            unprocessed_result = await db.execute(
                select(FavoriteVideo.bvid)
                .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid)
                .where(
                    FavoriteVideo.folder_id == folder.id,
                    VideoCache.is_processed.is_(False),
                )
            )
            for row in unprocessed_result.fetchall():
                bvid = row[0]
                if bvid not in added:
                    update_candidates.add(bvid)

        _folder_id = folder.id
        _folder_session_id = folder.session_id

        await db.commit()
        db.expire_all()

        # 增量同步下，update_candidates 中的出库视频不在本次 B站拉取结果中（video_map），
        # 需从 VideoCache 补全元数据，否则 _process_video 访问 video_map[bvid] 会 KeyError。
        missing_meta = update_candidates - set(video_map.keys())
        if missing_meta:
            missing_result = await db.execute(
                select(VideoCache).where(VideoCache.bvid.in_(list(missing_meta)))
            )
            for cache in missing_result.scalars():
                video_map[cache.bvid] = {
                    "title": cache.title or cache.bvid,
                    "cid": cache.cid,
                    "intro": cache.description,
                    "cover": cache.pic_url,
                    "duration": cache.duration,
                    "owner_name": cache.owner_name,
                    "owner_mid": cache.owner_mid,
                }
            # 仍无元数据的视频（VideoCache 不存在）无法处理，跳过并记录
            still_missing = missing_meta - set(video_map.keys())
            if still_missing:
                logger.warning(f"[{folder_id}] {len(still_missing)} 个视频缺少元数据，跳过: {still_missing}")
                update_candidates -= still_missing

        targets = list(added) + list(update_candidates)
        total_targets = len(targets)
        processed_targets = 0
        failed_targets = 0
        progress_lock = asyncio.Lock()

        try:
            from app.config import settings
            max_concurrent = settings.max_concurrent_ingestion
        except Exception:
            max_concurrent = 5
        max_concurrent = max(1, min(max_concurrent, 10))
        semaphore = asyncio.Semaphore(max_concurrent)

        if progress_callback:
            progress_callback("准备处理", processed_targets, total_targets)

        async def _process_video(bvid: str):
            nonlocal processed_targets, failed_targets
            meta = video_map[bvid]
            cache = None
            old_vector_deleted = False

            async with semaphore:
                video_trace = TraceContext(step=f"process_video:{bvid}")
                video_trace.__enter__()
                trace_logger.info(f"开始处理视频: {bvid}")
                if progress_callback:
                    progress_callback(meta.get("title", bvid), processed_targets, total_targets)

                # 适配 fetch_content 的单参进度回调签名，让 ASR 阶段也能更新入库进度
                def _fetch_progress_cb(msg: str):
                    if progress_callback:
                        progress_callback(msg, processed_targets, total_targets)

                try:
                    # ---- Phase 1: 读取缓存状态（持锁，快速 DB 查询）----
                    async with _db_lock:
                        video_db = db
                        global_count = await video_db.scalar(
                            select(func.count()).select_from(FavoriteVideo).where(FavoriteVideo.bvid == bvid)
                        )
                        result = await video_db.execute(select(VideoCache).where(VideoCache.bvid == bvid))
                        cache = result.scalar_one_or_none()
                        old_content = (cache.content or "").strip() if cache else ""
                        old_source = cache.content_source if cache else None
                        has_vectors = vector_presence.get(bvid)
                        if has_vectors is None:
                            has_vectors = await asyncio.to_thread(rag.has_video, bvid)

                        needs_fetch = _should_refresh_cache(cache, force_refresh)
                        needs_reindex_check = (
                            (global_count == 0) or cache is None
                            or not cache.is_processed or not has_vectors
                        )

                    # ---- Phase 2: ASR 内容获取（无锁，耗时网络/ASR 操作）----
                    # 关键：fetch_content 包含 ASR 转码（可能耗时 300s），
                    # 必须在 _db_lock 外执行，否则会阻塞所有其他视频的 DB 操作。
                    content = None
                    should_update_cache = False
                    should_reindex = False

                    if needs_fetch:
                        content = await content_fetcher.fetch_content(
                            bvid,
                            cid=meta["cid"],
                            title=meta["title"],
                            description=meta.get("intro"),
                            owner_name=meta.get("owner_name"),
                            owner_mid=meta.get("owner_mid"),
                            duration=meta.get("duration"),
                            progress_callback=_fetch_progress_cb,
                        )
                        new_text = (content.content or "").strip() if content else ""
                        new_source = content.source.value if content else None

                        if not old_content:
                            should_update_cache = True
                            should_reindex = True
                        elif new_source and _is_better_source(new_source, old_source):
                            should_update_cache = True
                            should_reindex = True
                        elif new_text and new_text != old_content:
                            should_update_cache = True
                            should_reindex = True

                    # ---- Phase 3: 向量化（无锁，耗时 RAG/ChromaDB 操作）----
                    # RAG 操作使用独立的 ChromaDB 连接，不依赖 SQLAlchemy session，
                    # 无需持有 _db_lock。放在锁外可让其他视频并发执行 DB 操作。
                    if needs_reindex_check or should_reindex:
                        if not content:
                            if _is_asr_cache_usable(cache):
                                content = VideoContent(
                                    bvid=bvid,
                                    title=meta["title"],
                                    content=(cache.content or "").strip(),
                                    source=ContentSource.ASR,
                                    outline=cache.outline_json,
                                    platform=cache.platform or "bilibili",
                                    description=meta.get("intro"),
                                    owner_name=meta.get("owner_name"),
                                    owner_mid=meta.get("owner_mid"),
                                    duration=meta.get("duration"),
                                )
                                trace_logger.info(f"[{bvid}] 使用缓存 ASR 内容重建向量")
                            else:
                                content = await content_fetcher.fetch_content(
                                    bvid,
                                    cid=meta["cid"],
                                    title=meta["title"],
                                    description=meta.get("intro"),
                                    owner_name=meta.get("owner_name"),
                                    owner_mid=meta.get("owner_mid"),
                                    duration=meta.get("duration"),
                                    progress_callback=_fetch_progress_cb,
                                )

                        prepared_docs = await asyncio.to_thread(rag.prepare_documents, content)
                        if not prepared_docs:
                            raise RuntimeError(
                                "内容预校验失败：未生成有效文档，保留原有向量不删除"
                            )
                        try:
                            await asyncio.to_thread(rag.delete_video, bvid)
                            old_vector_deleted = True
                        except Exception as e:
                            trace_logger.error(f"删除旧向量失败 [{bvid}]，跳过本次更新: {e}")
                            raise
                        chunks = await asyncio.to_thread(rag.add_video_content, content)
                        if chunks <= 0:
                            raise RuntimeError("未生成可写入的向量文档")

                        has_vectors_after = await asyncio.to_thread(rag.has_video, bvid)
                        if not has_vectors_after:
                            trace_logger.warning(
                                f"[{bvid}] 向量写入后 has_video 仍返回 False（可能 Mock 或最终一致性），"
                                f"块数={chunks}，视为写入成功不抛异常"
                            )

                        trace_logger.info(f"[{bvid}] 向量化完成，块数={chunks}")
                    else:
                        trace_logger.info(f"[{bvid}] 内容未变化或无需升级，跳过向量化")

                    # ---- Phase 4: 写回 DB（持锁，快速 DB 写入）----
                    async with _db_lock:
                        video_db = db
                        if content and should_update_cache and cache:
                            cache.content = content.content
                            cache.content_source = content.source.value
                            cache.outline_json = content.outline
                            trace_logger.info(f"[{bvid}] 已写入缓存: source={cache.content_source}")

                        if needs_reindex_check or should_reindex:
                            _set_cache_processing_result(cache)

                        exists_row = await video_db.execute(
                            select(FavoriteVideo.id).where(
                                FavoriteVideo.folder_id == _folder_id,
                                FavoriteVideo.bvid == bvid,
                            )
                        )
                        if exists_row.scalar_one_or_none() is None:
                            try:
                                video_db.add(FavoriteVideo(folder_id=_folder_id, bvid=bvid, is_selected=True))
                                await video_db.flush()
                            except IntegrityError:
                                logger.debug(f"FavoriteVideo 并发写入冲突 [{bvid}]，已忽略")
                        await video_db.commit()

                    async with progress_lock:
                        processed_targets += 1
                        if progress_callback:
                            progress_callback(meta["title"], processed_targets, total_targets)

                except Exception as e:
                    try:
                        async with _db_lock:
                            relation_db = db
                            exists_row = await relation_db.execute(
                                select(FavoriteVideo.id).where(
                                    FavoriteVideo.folder_id == _folder_id,
                                    FavoriteVideo.bvid == bvid,
                                )
                            )
                            if exists_row.scalar_one_or_none() is None:
                                try:
                                    relation_db.add(
                                        FavoriteVideo(
                                            folder_id=_folder_id,
                                            bvid=bvid,
                                            is_selected=True,
                                        )
                                    )
                                    await relation_db.flush()
                                    await relation_db.commit()
                                except IntegrityError:
                                    logger.debug(f"FavoriteVideo 并发写入冲突 [{bvid}]，已忽略")
                    except Exception:
                        pass
                    async with progress_lock:
                        failed_targets += 1
                        processed_targets += 1
                        if progress_callback:
                            progress_callback(meta["title"], processed_targets, total_targets)
                    try:
                        async with _db_lock:
                            video_db = db
                            cache = await video_db.scalar(
                                select(VideoCache).where(VideoCache.bvid == bvid)
                            )
                            _record_cache_error(cache, e)
                            if old_vector_deleted and cache:
                                cache.process_error = f"向量写入失败，旧向量已删除: {e}"
                            await video_db.commit()
                    except Exception:
                        pass
                    trace_logger.error(f"处理视频失败 [{bvid}]: {e}")
                finally:
                    video_trace.__exit__(None, None, None)

        try:
            tasks = [_process_video(bvid) for bvid in targets]
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            trace_logger.error(f"并发处理视频任务异常: {e}")

        if removed:
            failed_vector_deletes = []
            for bvid in removed:
                other_count = await db.scalar(
                    select(func.count())
                    .select_from(FavoriteVideo)
                    .where(
                        FavoriteVideo.bvid == bvid,
                        FavoriteVideo.folder_id != _folder_id,
                    )
                )
                if other_count == 0:
                    try:
                        await asyncio.to_thread(rag.delete_video, bvid)
                    except Exception as e:
                        trace_logger.error(f"删除向量失败 [{bvid}]: {e}")
                        failed_vector_deletes.append(bvid)
                        continue

            safe_to_remove = [bvid for bvid in removed if bvid not in failed_vector_deletes]
            if safe_to_remove:
                await db.execute(
                    delete(FavoriteVideo).where(
                        FavoriteVideo.folder_id == _folder_id,
                        FavoriteVideo.bvid.in_(safe_to_remove),
                    )
                )

        folder.last_sync_at = utcnow()

        # 增量同步游标更新：将 last_sync_at 推进到本次拉取到的最大 fav_time。
        # 无新增视频时 max_fav_time 保持为原 last_sync_at（已在初始化时赋值），游标不变。
        if max_fav_time is not None:
            try:
                await _upsert_sync_cursor(
                    db,
                    platform="bilibili",
                    folder_id=str(folder_id),
                    last_sync_at=max_fav_time,
                )
            except Exception as cursor_err:
                trace_logger.warning(f"[{folder_id}] 更新增量同步游标失败: {cursor_err}")

        await db.commit()

        async with _db_lock:
            _idx_db = db
            indexed_count = await _idx_db.scalar(
                select(func.count(func.distinct(FavoriteVideo.bvid)))
                .select_from(FavoriteVideo)
                .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid)
                .where(
                    FavoriteVideo.folder_id == _folder_id,
                    VideoCache.is_processed.is_(True),
                )
            )

        message = "同步完成"
        if failed_targets:
            message = f"同步未完成：{failed_targets} 个视频向量入库失败，可重新更新"

        trace_logger.info(f"收藏夹同步完成: {folder_id}, 入库={indexed_count}, 失败={failed_targets}")

        return {
            "folder_id": folder_id,
            "total": valid_count,
            "added": len(added),
            "removed": len(removed),
            "indexed": indexed_count or 0,
            "failed": failed_targets,
            "message": message,
            "last_sync_at": folder.last_sync_at,
        }
    finally:
        trace_ctx.__exit__(None, None, None)



@router.get("/stats")
async def get_knowledge_stats(
    session_id: str = Query(..., description="会话ID，需为有效会话"),
    db: AsyncSession = Depends(get_db),
):
    """获取知识库统计信息（合并 bilibili + douyin 两个集合）

    安全：仅统计当前 session 可见的视频，避免跨用户数据泄露。
    """
    session = await get_session(session_id, platform="bilibili")
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    try:
        # 收集当前用户可见的 bvid 集合，用于过滤统计
        target_session_ids = await _get_session_ids_for_user(db, session_id)
        bili_bvids_rows = await db.execute(
            select(FavoriteVideo.bvid)
            .join(FavoriteFolder, FavoriteFolder.id == FavoriteVideo.folder_id)
            .where(FavoriteFolder.session_id.in_(target_session_ids))
        )
        bili_bvids = {r[0] for r in bili_bvids_rows.fetchall() if r[0]}
        douyin_bvids_rows = await db.execute(
            select(FavoriteVideo.bvid)
            .join(FavoriteFolder, FavoriteFolder.id == FavoriteVideo.folder_id)
            .where(FavoriteFolder.session_id.like(f"douyin-{session_id}-%"))
        )
        douyin_bvids = {r[0] for r in douyin_bvids_rows.fetchall() if r[0]}

        rag_bili = get_rag_service("bilibili")
        rag_douyin = get_rag_service("douyin")
        stats_bili = await asyncio.to_thread(rag_bili.get_collection_stats)
        stats_douyin = await asyncio.to_thread(rag_douyin.get_collection_stats)
        # 按 bvid 过滤：仅计入当前用户可见的视频
        def _filter(stats, allowed_bvids):
            if not stats:
                return {}
            stats = dict(stats)
            # 修正 video_count：按可见 bvid 数计
            stats["total_videos"] = len(allowed_bvids)
            # total_chunks 难以按 bvid 精确过滤，保留原值但标注为全局值
            return stats
        stats_bili = _filter(stats_bili, bili_bvids)
        stats_douyin = _filter(stats_douyin, douyin_bvids)
        # 合并两个集合的统计：数值相加，列表/字典合并
        merged = dict(stats_bili)
        for key, value in (stats_douyin or {}).items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            elif isinstance(value, list):
                merged[key] = (merged.get(key) or []) + value
            elif isinstance(value, dict):
                base = dict(merged.get(key) or {})
                base.update(value)
                merged[key] = base
            else:
                # 字符串等冲突时保留 bilibili 的值
                merged.setdefault(key, value)
        return merged
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        raise HTTPException(status_code=500, detail="获取统计信息失败，请稍后重试")


@router.get("/folders/status", response_model=List[FolderStatus])
async def get_folder_status(
    session_id: str = Query(..., description="会话ID"),
    db: AsyncSession = Depends(get_db),
):
    """获取收藏夹入库状态（跨 Session 查找同一用户的数据）"""
    
    # 1. 先查当前 Session 对应的用户 MID（仅限 bilibili 平台）
    result = await db.execute(
        select(UserSession.bili_mid).where(
            UserSession.session_id == session_id,
            UserSession.platform == "bilibili",
        )
    )
    mid = result.scalar()
    
    target_session_ids = [session_id]
    
    if mid:
        # 2. 如果有 MID，查找该用户所有的 bilibili Session ID
        result = await db.execute(
            select(UserSession.session_id).where(
                UserSession.bili_mid == mid,
                UserSession.platform == "bilibili",
            )
        )
        target_session_ids = [row[0] for row in result.fetchall()]
    
    # 3. 查询所有关联 Session 的收藏夹状态
    # 使用 group_by media_id 来去重，取最新的那个
    rows = await db.execute(
        select(FavoriteFolder.id, FavoriteFolder.media_id, FavoriteFolder.last_sync_at)
        .where(FavoriteFolder.session_id.in_(target_session_ids))
        .order_by(FavoriteFolder.updated_at.desc())
    )
    
    # 手动按 media_id 去重，保留最新的
    folders_map = {}
    for row in rows.fetchall():
        fid, media_id, last_sync = row
        if media_id not in folders_map:
            folders_map[media_id] = (fid, last_sync)
            
    if not folders_map:
        return []

    folder_ids = [v[0] for v in folders_map.values()]

    # 4. 按 Chroma 实际数据校准并统计入库状态
    relations = await db.execute(
        select(FavoriteVideo.folder_id, FavoriteVideo.bvid, VideoCache)
        .join(VideoCache, VideoCache.bvid == FavoriteVideo.bvid)
        .where(FavoriteVideo.folder_id.in_(folder_ids))
    )
    rag = get_rag_service()
    all_rows = relations.fetchall()

    # 批量查询向量库，避免逐个 has_video 的 N+1 调用
    unique_bvids = list({row[1] for row in all_rows if row[1]})
    vector_presence: dict[str, bool] = await asyncio.to_thread(rag.has_videos, unique_bvids)

    indexed_map: dict[int, int] = {}
    failed_map: dict[int, int] = {}
    state_changed = False
    for folder_id, bvid, cache in all_rows:
        has_vectors = vector_presence.get(bvid, False)
        # 仅当 is_processed=True 但向量缺失时才标记为异常（数据不一致）。
        # is_processed=False 且 process_error=None 是出库后的正常状态，不应标记为失败。
        if not has_vectors and cache.is_processed:
            _set_cache_processing_result(cache, RuntimeError("向量数据缺失，等待重新入库"))
            state_changed = True
        if has_vectors and cache.is_processed:
            indexed_map[folder_id] = indexed_map.get(folder_id, 0) + 1
        if cache.process_error:
            failed_map[folder_id] = failed_map.get(folder_id, 0) + 1

    if state_changed:
        await db.commit()

    result = []
    for media_id, (folder_id, last_sync_at) in folders_map.items():
        # 读取有效视频数（过滤失效后的口径）
        folder_row = await db.execute(
            select(FavoriteFolder.media_count).where(FavoriteFolder.id == folder_id)
        )
        media_count = folder_row.scalar()
        result.append(
            FolderStatus(
                media_id=media_id,
                indexed_count=indexed_map.get(folder_id, 0),
                failed_count=failed_map.get(folder_id, 0),
                media_count=media_count,
                last_sync_at=last_sync_at,
            )
        )
    return result


@router.post("/folders/sync", response_model=List[SyncResult])
async def sync_folders(
    request: SyncRequest,
    session_id: str = Query(..., description="会话ID"),
    db: AsyncSession = Depends(get_db),
):
    """同步收藏夹到向量库"""
    session = await get_session(session_id, platform="bilibili")
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    cookies = session.get("cookies", {})
    user_info = session.get("user_info", {})

    bili = BilibiliService(
        sessdata=cookies.get("SESSDATA"),
        bili_jct=cookies.get("bili_jct"),
        dedeuserid=cookies.get("DedeUserID"),
    )
    rag = get_rag_service()
    asr_service = ASRService()
    content_fetcher = ContentFetcher(bili, asr_service)

    try:
        folder_ids = request.folder_ids or []
        if not folder_ids:
            mid = user_info.get("mid") or cookies.get("DedeUserID")
            if not mid:
                raise HTTPException(status_code=400, detail="无法获取用户信息")
            folders = await bili.get_user_favorites(mid=mid)
            folder_ids = [folder.get("id") for folder in folders if folder.get("id")]

        results: List[SyncResult] = []
        for folder_id in folder_ids:
            try:
                result = await _sync_folder(
                    db,
                    bili,
                    rag,
                    content_fetcher,
                    session_id,
                    folder_id,
                )
                results.append(SyncResult(**result))
            except Exception as e:
                logger.error(f"同步收藏夹失败 [{folder_id}]: {e}")
                results.append(
                    SyncResult(
                        folder_id=folder_id,
                        total=0,
                        added=0,
                        removed=0,
                        indexed=0,
                        failed=1,
                        message=f"同步失败: {e}",
                        last_sync_at=None,
                    )
                )

        return results
    finally:
        await bili.close()


@router.post("/build")
async def build_knowledge_base(
    request: BuildRequest,
    background_tasks: BackgroundTasks,
    session_id: str = Query(..., description="会话ID"),
):
    """构建知识库（后台任务）"""
    set_operation_type("user_action")
    session = await get_session(session_id, platform="bilibili")
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    # 去重键：同一 session 同时只能有一个 build_knowledge_base 任务
    dedup_video_id = f"build:{session_id}"
    metadata = {
        "platform": "bilibili",
        "session_id": session_id,
        "total_videos": 0,
        "processed_videos": 0,
        "total_folders": len(request.folder_ids),
        "processed_folders": 0,
        "current_folder_id": None,
        "current_folder_title": None,
        "current_video_title": None,
        "message": "",
        "succeeded": 0,
        "failed": 0,
        # 保存原始参数，供 auto_retry 重启时恢复
        "folder_ids": list(request.folder_ids),
        "exclude_bvids": list(request.exclude_bvids or []),
    }
    task_id = await task_tracker.create_task_if_not_exists(
        task_type="build_knowledge_base",
        video_id=dedup_video_id,
        metadata=metadata,
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有构建任务正在运行，请等待完成后再试")

    background_tasks.add_task(
        _build_knowledge_base_task,
        task_id,
        session_id,
        session,
        request.folder_ids,
        request.exclude_bvids or [],
    )

    return {"task_id": task_id, "message": "构建任务已启动"}


async def _build_knowledge_base_task(
    task_id: str,
    session_id: str,
    session: dict,
    folder_ids: List[int],
    exclude_bvids: List[str],
):
    """后台构建任务"""
    set_operation_type("background_task")
    cookies = session.get("cookies", {})

    try:
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.RUNNING,
            step="同步收藏夹...",
        )

        bili = None
        try:
            bili = BilibiliService(
                sessdata=cookies.get("SESSDATA"),
                bili_jct=cookies.get("bili_jct"),
                dedeuserid=cookies.get("DedeUserID"),
            )
            asr_service = ASRService()
            content_fetcher = ContentFetcher(bili, asr_service)
            rag = get_rag_service()

            total_folders = len(folder_ids)
            await task_tracker.update_task(
                task_id,
                metadata={"total_folders": total_folders},
            )
            if total_folders == 0:
                await task_tracker.update_task(
                    task_id,
                    status=TaskStatus.SUCCESS,
                    progress=100,
                    metadata={"message": "没有需要处理的收藏夹"},
                )
                return

            total_added = 0
            total_removed = 0
            total_failed = 0
            total_indexed = 0

            # 保存 asyncio.create_task 的强引用，避免 fire-and-forget 任务被 GC 回收。
            # 定义在 for 循环外，确保跨文件夹追踪所有 pending 进度更新。
            _pending_progress_tasks: set = set()

            async with get_db_context() as db:
                for idx, folder_id in enumerate(folder_ids, start=1):
                    await task_tracker.update_task(
                        task_id,
                        step=f"同步收藏夹 {folder_id}",
                        metadata={
                            "current_folder_id": folder_id,
                            "current_folder_title": f"收藏夹 {folder_id}",
                            "current_video_title": None,
                            "processed_folders": idx - 1,
                            "processed_videos": 0,
                            "total_videos": 0,
                        },
                    )

                    def progress_cb(title: str, processed_count: int = 0, total_count: int = 0):
                        # progress_callback 是同步函数，无法 await task_tracker.update_task。
                        # 通过事件循环调度异步更新，避免阻塞入库主流程。
                        metadata = {
                            "current_video_title": title,
                        }
                        if total_count:
                            metadata["total_videos"] = total_count
                        if processed_count or processed_count == 0:
                            metadata["processed_videos"] = processed_count
                        # 计算进度百分比
                        pct = int(processed_count / total_count * 100) if total_count else 0
                        task = asyncio.create_task(
                            task_tracker.update_task(
                                task_id, step=f"处理: {title}", progress=pct, metadata=metadata
                            )
                        )
                        _pending_progress_tasks.add(task)
                        task.add_done_callback(_pending_progress_tasks.discard)

                    try:
                        result = await _sync_folder(
                            db,
                            bili,
                            rag,
                            content_fetcher,
                            session_id,
                            folder_id,
                            exclude_bvids=set(exclude_bvids),
                            progress_callback=progress_cb,
                        )
                    except Exception as e:
                        trace_logger.error(f"收藏夹 {folder_id} 同步失败: {e}")
                        result = {"succeeded": 0, "failed": 0, "total": 0}
                        total_failed += 1  # 收藏夹级失败计数

                    await task_tracker.update_task(
                        task_id,
                        metadata={"processed_folders": idx},
                    )
                    total_added += result.get("added", 0)
                    total_removed += result.get("removed", 0)
                    total_failed += result.get("failed", 0)
                    total_indexed += result.get("indexed", 0)

            # 等待所有 fire-and-forget 进度更新完成，再写入最终状态。
            # 否则 late-arriving 的进度更新会在终态写入后覆盖 step/progress 字段，
            # 且大量 pending 任务竞争 _write_lock 会延迟终态写入和前端轮询读取。
            if _pending_progress_tasks:
                await asyncio.gather(*_pending_progress_tasks, return_exceptions=True)
                _pending_progress_tasks.clear()

            final_status = TaskStatus.FAILED if total_failed else TaskStatus.SUCCESS
            if total_failed:
                final_message = f"同步未完成：{total_failed} 个视频向量入库失败，可重新更新"
            else:
                # added/removed 统计的是收藏关系变更，indexed 是实际已入库的视频总数
                # 当 added=0 但 indexed>0 时，说明是更新已有视频的向量（而非新增收藏）
                final_message = (
                    f"同步完成：已入库 {total_indexed} 个视频"
                    + (f"，新增收藏 {total_added}" if total_added else "")
                    + (f"，移除 {total_removed}" if total_removed else "")
                )
            await task_tracker.update_task(
                task_id,
                status=final_status,
                progress=100,
                step="失败" if total_failed else "完成",
                metadata={
                    "processed_folders": total_folders,
                    "current_folder_id": None,
                    "current_folder_title": None,
                    "current_video_title": None,
                    "message": final_message,
                    "total_added": total_added,
                    "total_removed": total_removed,
                    "total_indexed": total_indexed,
                },
            )

            logger.info(f"知识库构建结束: 已入库 {total_indexed}，新增收藏 {total_added}，移除 {total_removed}，失败 {total_failed}")
        finally:
            if bili is not None:
                try:
                    await bili.close()
                except Exception as close_err:
                    logger.warning(f"关闭 BilibiliService 失败: {close_err}")

    except ValueError as e:
        # 配置缺失的 ValueError 直接透传中文消息
        error_msg = str(e)
        logger.error(f"构建任务失败（配置错误）: {error_msg}")
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error_message=error_msg,
            error_stage=ErrorStage.CONFIG.value,
            metadata={"message": error_msg},
        )
    except Exception as e:
        error_msg = str(e)
        # OpenAI SDK 英文错误中文化
        if "api_key client option must be set" in error_msg:
            error_msg = "未配置 LLM API Key（openai_api_key），请在设置中配置后再入库"
        elif "Incorrect API key provided" in error_msg:
            error_msg = "LLM API Key 无效，请检查设置中的 openai_api_key"
        elif "no file named model.safetensors" in error_msg or "no file named pytorch_model.bin" in error_msg:
            error_msg = "本地向量模型文件不完整（缺少权重文件），请在模型市场重新下载向量模型"
        logger.error(f"构建任务失败: {error_msg} (原始: {e})")
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error_message=error_msg,
            error_stage="unknown",
            metadata={"message": error_msg},
        )


@router.get("/build/status/{task_id}", response_model=BuildStatus, deprecated=True)
async def get_build_status(
    task_id: str,
    session_id: str = Query(..., description="会话ID，需为任务归属者"),
):
    """获取构建任务状态 (deprecated: 请改用 GET /api/tasks/{task_id})

    安全：校验调用方 session_id 与任务归属一致，避免枚举他人 task_id 窃取视频标题。
    """
    # 顺手清理已过期的构建任务（兼容层 no-op，真实清理由 task_tracker.cleanup_expired_tasks）
    _prune_expired_build_tasks()

    task = await task_tracker.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 安全校验：仅任务归属者可查询（session_id 存放在 metadata 与 TaskInfo.session_id）
    task_owner = (task.get("metadata") or {}).get("session_id") or task.get("session_id")
    if task_owner and session_id != task_owner:
        # 不回显"存在但无权"，统一返回 404 避免枚举
        raise HTTPException(status_code=404, detail="任务不存在")
    return task_info_to_build_status(task_id, task)


@router.delete("/clear")
async def clear_knowledge_base(
    session_id: str = Query(..., description="会话ID，需为有效会话"),
    db: AsyncSession = Depends(get_db),
):
    """清空当前用户的知识库（仅删除该用户 session 关联的向量，不影响他人数据）"""
    session = await get_session(session_id, platform="bilibili")
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    try:
        # 获取该用户所有 session_id，按 session_id 维度删除向量
        target_session_ids = await _get_session_ids_for_user(db, session_id)
        # 收集这些 session 下所有 (bvid, platform)，按 platform 路由到对应 RAG 集合
        # 注意：原实现只取 bvid 并默认调用 bilibili 集合的 delete_video，
        # 导致抖音视频的向量（在 douyin_videos 集合）无法被清理，产生孤儿向量。
        bvid_rows = await db.execute(
            select(VideoCache.bvid, VideoCache.platform).distinct().join(
                FavoriteVideo, FavoriteVideo.bvid == VideoCache.bvid
            ).join(
                FavoriteFolder, FavoriteFolder.id == FavoriteVideo.folder_id
            ).where(FavoriteFolder.session_id.in_(target_session_ids))
        )
        # fetchall 一次后游标即关闭，避免重复消费
        bvid_platforms: list[tuple[str, Optional[str]]] = list(bvid_rows.fetchall())
        bvids = [bp[0] for bp in bvid_platforms]

        # 事务一致性：先删除向量，向量全部成功后才删除 DB。
        # 若向量删除部分失败，DB 仍保留记录，避免产生孤儿向量（DB 已删但向量残留）。
        # 失败的 bvid 收集后返回给用户，让其重试。
        failed_bvids: list[str] = []
        skipped_bvids: list[str] = []
        deleted = 0
        for bvid, platform in bvid_platforms:
            # 按 VideoCache.platform 路由到正确的 RAG 集合
            if not platform:
                # platform 为空（旧数据），无法确定目标集合。
                # 为避免误路由到单一集合导致另一集合残留孤儿向量，
                # 这里对 bilibili + douyin 双集合都执行 delete_video（无此 bvid 时为空操作）。
                logger.warning(f"bvid={bvid} 的 platform 为空，执行 bilibili + douyin 双集合清理")
                skipped_bvids.append(bvid)
                for _p in ("bilibili", "douyin"):
                    try:
                        await asyncio.to_thread(get_rag_service(_p).delete_video, bvid)
                    except Exception:
                        pass  # 集合中无此 bvid 属正常
                deleted += 1
                continue
            rag = get_rag_service(platform)
            try:
                await asyncio.to_thread(rag.delete_video, bvid)
                deleted += 1
            except Exception as e:
                logger.warning(f"删除向量失败 [{bvid}/{platform}]: {e}")
                failed_bvids.append(bvid)

        # 仅当所有向量都成功删除时，才删除 DB 记录，避免孤儿向量
        if failed_bvids:
            logger.error(
                f"清空知识库中止：{len(failed_bvids)} 个视频向量删除失败，DB 保留以避免孤儿向量: {failed_bvids[:5]}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"有 {len(failed_bvids)} 个视频向量删除失败，已中止清空。请重试或联系管理员。"
            )

        # 所有向量已删除，安全删除 DB 记录
        await db.execute(
            delete(FavoriteVideo).where(
                FavoriteVideo.folder_id.in_(
                    select(FavoriteFolder.id).where(
                        FavoriteFolder.session_id.in_(target_session_ids)
                    )
                )
            )
        )
        await db.execute(
            delete(VideoCache).where(VideoCache.bvid.in_(bvids))
        )
        await db.commit()
        return {
            "message": f"已清空 {deleted} 个视频的知识库",
            "skipped_bvids": skipped_bvids,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"清空知识库失败: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="清空知识库失败，请稍后重试")


@router.delete("/video/{bvid}")
async def delete_video_from_knowledge(
    bvid: str,
    platform: str = Query("bilibili", description="平台：bilibili 或 douyin"),
    session_id: str = Query(..., description="会话ID，需为有效会话"),
    db: AsyncSession = Depends(get_db),
):
    """从知识库中删除指定视频（向量 + 缓存 + 收藏关系）

    需校验该视频属于当前用户的收藏夹，避免越权删除他人数据。
    """
    # 先查询 video 的 platform，动态校验 session 平台
    # 避免硬编码 platform="bilibili" 导致抖音视频删除时 session 校验失败
    cache_probe = await db.scalar(select(VideoCache).where(VideoCache.bvid == bvid))
    video_platform = cache_probe.platform if cache_probe else platform
    session = await get_session(session_id, platform=video_platform)
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    try:
        # 按 platform 路由到对应的 RAG 集合，避免抖音视频向量从 bilibili 集合删除（无效果）
        rag = get_rag_service(platform)
        # 先查 VideoCache，按平台过滤避免误删
        cache = await db.scalar(
            select(VideoCache).where(
                VideoCache.bvid == bvid,
                VideoCache.platform == platform,
            )
        )
        if not cache:
            raise HTTPException(status_code=404, detail="视频不存在或不属于当前会话")

        # 校验该视频属于当前用户
        target_session_ids = await _get_session_ids_for_user(db, session_id)
        if platform == "douyin":
            # 抖音按调用方 session_id 派生 user_scope，不再依赖全局 douyin-active 单例
            user_scope = session_id
            owned = await db.scalar(
                select(FavoriteFolder.id)
                .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
                .where(
                    FavoriteVideo.bvid == bvid,
                    FavoriteFolder.session_id.like(f"douyin-{user_scope}-%"),
                )
            )
        else:
            owned = await db.scalar(
                select(FavoriteFolder.id)
                .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
                .where(
                    FavoriteVideo.bvid == bvid,
                    FavoriteFolder.session_id.in_(target_session_ids),
                )
            )
        if not owned:
            raise HTTPException(status_code=403, detail="无权删除该视频")

        # 在删除 FavoriteVideo 之前，记录受影响的收藏夹 media_id，
        # 删除完成后重置这些文件夹的增量同步游标，使下次同步走全量拉取，
        # 否则增量同步无法发现已被删除的视频（它仍在 B站收藏夹中，但本地已无记录）。
        affected_media_ids_result = await db.execute(
            select(FavoriteFolder.media_id)
            .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
            .where(FavoriteVideo.bvid == bvid)
        )
        affected_media_ids = [row[0] for row in affected_media_ids_result.fetchall()]

        # 删除向量：失败时必须中止 DB 删除，避免出现 DB 已删但向量残留的不一致状态
        try:
            await asyncio.to_thread(rag.delete_video, bvid)
        except Exception as e:
            logger.error(f"删除向量失败 [{bvid}/{platform}]: {e}")
            raise HTTPException(status_code=500, detail=f"删除向量库数据失败: {e}")
        # 只有向量删除成功才继续删除 DB 记录
        # 删除收藏关系：必须限定 folder 归属，避免删除其他用户/其他 folder 对同一 bvid 的收藏
        if platform == "douyin":
            await db.execute(
                delete(FavoriteVideo).where(
                    FavoriteVideo.bvid == bvid,
                    FavoriteVideo.folder_id.in_(
                        select(FavoriteFolder.id).where(
                            FavoriteFolder.session_id.like(f"douyin-{user_scope}-%")
                        )
                    ),
                )
            )
        else:
            await db.execute(
                delete(FavoriteVideo).where(
                    FavoriteVideo.bvid == bvid,
                    FavoriteVideo.folder_id.in_(
                        select(FavoriteFolder.id).where(
                            FavoriteFolder.session_id.in_(target_session_ids)
                        )
                    ),
                )
            )
        # 删除视频缓存
        await db.delete(cache)

        # 重置受影响收藏夹的增量同步游标，使下次入库走全量拉取。
        # 否则增量同步只拉取 fav_time > last_sync_at 的新视频，
        # 无法发现"已被删除但仍收藏在 B站"的视频，导致重新入库时入库=0。
        if affected_media_ids:
            from app.models import SyncCursor
            for media_id in affected_media_ids:
                await db.execute(
                    delete(SyncCursor).where(
                        SyncCursor.platform == platform,
                        SyncCursor.folder_id == str(media_id),
                    )
                )
            logger.info(f"已重置 {len(affected_media_ids)} 个收藏夹的同步游标 (视频 {bvid} 被删除)")

        await db.commit()
        return {"message": f"已删除视频 {bvid}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除视频失败: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="删除视频失败，请稍后重试")


@router.delete("/video/{bvid}/rag")
async def remove_video_from_rag(
    bvid: str,
    platform: str = Query("bilibili", description="平台：bilibili 或 douyin"),
    session_id: str = Query(..., description="会话ID，需为有效会话"),
    db: AsyncSession = Depends(get_db),
):
    """从 RAG 向量库中移除指定视频（出库），保留 VideoCache 元数据。

    与全量删除不同，此端点仅删除向量数据并将 is_processed 置为 False，
    保留 VideoCache 和 FavoriteVideo 记录，便于后续重新入库。
    """
    # 先查询 video 的 platform，动态校验 session 平台
    # 避免硬编码 platform="bilibili" 导致抖音视频出库时 session 校验失败
    cache_probe = await db.scalar(select(VideoCache).where(VideoCache.bvid == bvid))
    video_platform = cache_probe.platform if cache_probe else platform
    session = await get_session(session_id, platform=video_platform)
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    try:
        rag = get_rag_service(platform)
        cache = await db.scalar(
            select(VideoCache).where(
                VideoCache.bvid == bvid,
                VideoCache.platform == platform,
            )
        )
        if not cache:
            raise HTTPException(status_code=404, detail="视频不存在或不属于当前会话")

        # 校验归属
        target_session_ids = await _get_session_ids_for_user(db, session_id)
        if platform == "douyin":
            user_scope = session_id
            owned = await db.scalar(
                select(FavoriteFolder.id)
                .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
                .where(
                    FavoriteVideo.bvid == bvid,
                    FavoriteFolder.session_id.like(f"douyin-{user_scope}-%"),
                )
            )
        else:
            owned = await db.scalar(
                select(FavoriteFolder.id)
                .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
                .where(
                    FavoriteVideo.bvid == bvid,
                    FavoriteFolder.session_id.in_(target_session_ids),
                )
            )
        if not owned:
            raise HTTPException(status_code=403, detail="无权操作该视频")

        # 仅删除向量，保留元数据：失败时中止，避免 is_processed 被误置为 False 但向量残留
        try:
            await asyncio.to_thread(rag.delete_video, bvid)
        except Exception as e:
            logger.error(f"出库-删除向量失败 [{bvid}/{platform}]: {e}")
            raise HTTPException(status_code=500, detail=f"删除向量库数据失败: {e}")

        cache.is_processed = False
        cache.process_error = None
        await db.commit()
        return {"message": f"已出库 {bvid}，元数据已保留"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"出库失败: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="出库失败，请稍后重试")


class VideoContentUpdate(BaseModel):
    """视频 ASR 文本编辑请求体"""
    content: str  # 编辑后的 ASR 文本


@router.put("/video/{video_id}/content")
async def update_video_content(
    video_id: str,
    body: VideoContentUpdate,
    db: AsyncSession = Depends(get_db),
):
    """编辑 ASR 文本后，重新切片+向量化+FTS5 upsert。

    流程：
      1. 查 VideoCache 确认视频存在，不存在返回 404
      2. 更新 VideoCache.content 为编辑后的文本
      3. 删除旧向量（rag.delete_video，best-effort）
      4. 重新向量化（rag.add_video_content，用新 content 构造 VideoContent）
      5. FTS5 upsert（hybrid_retriever.upsert，关键词召回通道）
      6. 提交 DB 事务
      7. 返回 {success, message}

    平台路由：根据 VideoCache.platform 选择对应的 RAG 集合（bilibili / douyin），
    兼容旧数据 platform 为空时默认走 bilibili 集合。
    """
    from app.services.hybrid_retriever import get_hybrid_retriever

    # 1. 查 VideoCache 确认视频存在
    cache = await db.scalar(select(VideoCache).where(VideoCache.bvid == video_id))
    if not cache:
        raise HTTPException(status_code=404, detail="视频不存在")

    # 空内容校验：避免写入空文本导致向量库/FTS5 出现空文档
    new_content = (body.content or "").strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="内容不能为空")

    # 按 platform 路由到对应 RAG 集合（默认 bilibili，兼容旧数据 platform 为空）
    platform = cache.platform or "bilibili"
    rag = get_rag_service(platform)

    # 构造 VideoContent 用于重新切片+向量化；保留原内容来源，缺失时按 ASR 处理
    try:
        source_enum = ContentSource(cache.content_source) if cache.content_source else ContentSource.ASR
    except ValueError:
        source_enum = ContentSource.ASR

    video_content = VideoContent(
        bvid=video_id,
        title=cache.title or video_id,
        content=new_content,
        source=source_enum,
        outline=cache.outline_json,
        description=cache.description,
        owner_name=cache.owner_name,
        owner_mid=cache.owner_mid,
        duration=cache.duration,
        platform=platform,
    )

    try:
        # 2. 更新 VideoCache.content
        cache.content = new_content
        cache.content_source = source_enum.value

        # 3. 删除旧向量（best-effort，无旧向量时为空操作，失败不阻塞重新向量化）
        try:
            await asyncio.to_thread(rag.delete_video, video_id)
        except Exception as e:
            logger.warning(f"删除旧向量失败 [{video_id}]，继续重新向量化: {e}")

        # 4. 重新向量化（add_video_content 内部会切片+写向量库）
        chunks = await asyncio.to_thread(rag.add_video_content, video_content)
        if chunks <= 0:
            raise RuntimeError("未生成可写入的向量文档")

        # 5. FTS5 upsert（显式调用，确保关键词召回通道与编辑后内容同步）
        await asyncio.to_thread(get_hybrid_retriever().upsert, video_id, new_content)

        # 标记为已处理，清除历史错误状态
        _set_cache_processing_result(cache)

        # 6. 提交 DB 事务
        await db.commit()
        # 7. 返回结果
        return {"success": True, "message": "已更新并重新向量化", "chunks": chunks}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"编辑视频内容失败 [{video_id}]: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"更新失败: {e}")


@router.post("/video/{bvid}/export")
async def export_video_markdown(
    bvid: str,
    payload: MarkdownExportRequest,
    session_id: str = Query(..., description="会话ID"),
    db: AsyncSession = Depends(get_db),
):
    """导出当前用户已入库视频的 Markdown 内容。

    安全：强制要求 session_id，并校验视频归属于调用方 session。
    """
    # bvid 格式校验：防止 Content-Disposition header 注入（拒绝 \r\n " 等特殊字符）
    # 允许 B 站 BV 开头 + 字母数字，或抖音纯数字 aweme_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", bvid):
        raise HTTPException(status_code=400, detail="视频 ID 格式不合法")

    session = await get_session(session_id, platform="bilibili")
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    # 收集当前用户可访问的所有文件夹 session_id（B站）
    accessible_session_ids: List[str] = await _get_session_ids_for_user(db, session_id)

    # 抖音文件夹 session_id 模式：douyin-{user_scope}-%
    # 用调用方 session_id 派生 user_scope，不再依赖全局 douyin-active 单例
    douyin_user_scope = session_id

    # 先查 VideoCache（按 bvid）
    video = await db.scalar(select(VideoCache).where(VideoCache.bvid == bvid))
    if video is None:
        raise HTTPException(status_code=404, detail="视频尚未入库或不属于当前会话")

    # 校验该视频属于当前用户的某个文件夹
    folder_query = (
        select(FavoriteFolder.id)
        .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
        .where(FavoriteVideo.bvid == bvid)
    )
    if video.platform == "douyin":
        # 抖音：按 user_scope 模糊匹配
        folder_query = folder_query.where(
            FavoriteFolder.session_id.like(f"douyin-{douyin_user_scope}-%")
        )
    else:
        # B 站：按 session_ids 精确匹配
        if not accessible_session_ids:
            raise HTTPException(status_code=404, detail="视频尚未入库或不属于当前会话")
        folder_query = folder_query.where(
            FavoriteFolder.session_id.in_(accessible_session_ids)
        )
    owned = (await db.execute(folder_query.limit(1))).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status_code=404, detail="视频尚未入库或不属于当前会话")

    if not (video.content or "").strip():
        raise HTTPException(status_code=409, detail="视频尚无可导出的字幕或转写内容")

    cancel_event = _start_operation(payload.operation_id, session_id)
    cancel_check = cancel_event.is_set if cancel_event else None
    try:
        ensure_not_cancelled(cancel_check)
        ai_content = None
        if payload.mode == "ai":
            ai_content = await asyncio.to_thread(
                organize_video_content,
                video.title,
                video.content,
                cancel_check=cancel_check,
            )
        ensure_not_cancelled(cancel_check)
        markdown = build_video_markdown(video, ai_content=ai_content)
        # bvid 已通过格式校验，可安全拼入 header
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{bvid}.md"'},
        )
    except OperationCancelled:
        logger.info(f"视频 Markdown 导出已取消 [{bvid}]")
        raise HTTPException(status_code=409, detail="操作已取消")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"导出视频 Markdown 失败 [{bvid}]: {e}")
        raise HTTPException(status_code=500, detail="导出失败，请稍后重试")
    finally:
        _finish_operation(payload.operation_id, session_id)


@router.post("/video/{bvid}/ingest")
async def ingest_single_video(
    bvid: str,
    payload: SingleVideoIngestRequest,
    session_id: str = Query(..., description="会话ID"),
    force_refresh: bool = Query(False, description="强制刷新缓存并重新入库"),
    db: AsyncSession = Depends(get_db),
):
    """将当前用户收藏夹中的单个视频写入知识库。"""
    session = await get_session(session_id, platform="bilibili")
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    cookies = session.get("cookies", {})
    bili = BilibiliService(
        sessdata=cookies.get("SESSDATA"),
        bili_jct=cookies.get("bili_jct"),
        dedeuserid=cookies.get("DedeUserID"),
    )
    cancel_event = _start_operation(payload.operation_id, session_id)
    cancel_check = cancel_event.is_set if cancel_event else None
    try:
        asr_service = ASRService(cancel_check=cancel_check)
        content_fetcher = ContentFetcher(bili, asr_service, cancel_check=cancel_check)
        cache = await _ingest_single_video(
            db,
            bili,
            # 该端点当前仅 B 站流程调用（cookies 来自 B 站 session）。
            # 若后续支持抖音单视频入库，需新增 platform 参数并按平台路由集合。
            get_rag_service("bilibili"),
            content_fetcher,
            session_id,
            payload.folder_id,
            bvid,
            cancel_check,
            force_refresh=force_refresh,
        )
        return {
            "bvid": cache.bvid,
            "title": cache.title,
            "message": "单视频入库完成",
        }
    except OperationCancelled:
        await db.rollback()
        logger.info(f"单视频入库已取消 [{bvid}]")
        raise HTTPException(status_code=409, detail="操作已取消")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"单视频入库失败 [{bvid}]: {e}")
        raise HTTPException(status_code=500, detail="单视频入库失败，请稍后重试")
    finally:
        _finish_operation(payload.operation_id, session_id)
        await bili.close()


@router.post("/video/{bvid}/ingest/stream")
async def ingest_single_video_stream(
    bvid: str,
    payload: SingleVideoIngestRequest,
    session_id: str = Query(..., description="会话ID"),
    force_refresh: bool = Query(False, description="强制刷新缓存并重新入库"),
    db: AsyncSession = Depends(get_db),
):
    """将单个视频写入知识库（SSE 推送步骤进度）。

    使用 text/event-stream 推送事件：
      event: progress
      data: {"step": "asr", "status": "running", "message": "..."}
    最终事件为 done / error。
    保留 POST /knowledge/video/{bvid}/ingest 同步端点供旧客户端使用。
    """
    session = await get_session(session_id, platform="bilibili")
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    cookies = session.get("cookies", {})
    cancel_event = _start_operation(payload.operation_id, session_id)
    cancel_check = cancel_event.is_set if cancel_event else None

    async def _event_stream():
        # 服务端通过 queue 与回调通信：progress_callback 是同步函数，
        # 内部把事件放入 asyncio 队列，主循环读取并 yield 出去。
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
            bili = BilibiliService(
                sessdata=cookies.get("SESSDATA"),
                bili_jct=cookies.get("bili_jct"),
                dedeuserid=cookies.get("DedeUserID"),
            )
            try:
                asr_service = ASRService(cancel_check=cancel_check)
                content_fetcher = ContentFetcher(bili, asr_service, cancel_check=cancel_check)
                await _ingest_single_video(
                    db,
                    bili,
                    get_rag_service("bilibili"),
                    content_fetcher,
                    session_id,
                    payload.folder_id,
                    bvid,
                    cancel_check,
                    progress_callback=_on_progress,
                    force_refresh=force_refresh,
                )
                # 成功完成事件（若未由 _ingest_single_video 推送 done）
                await queue.put({"step": "done", "status": "completed", "message": "单视频入库完成"})
            except OperationCancelled:
                await queue.put({"step": "error", "status": "cancelled", "message": "操作已取消"})
                await db.rollback()
            except HTTPException as he:
                await queue.put({"step": "error", "status": "failed", "message": str(he.detail)})
            except Exception as e:
                logger.error(f"SSE 单视频入库失败 [{bvid}]: {e}")
                await queue.put({"step": "error", "status": "failed", "message": f"入库失败: {e}"})
            finally:
                _finish_operation(payload.operation_id, session_id)
                try:
                    await bili.close()
                except Exception as close_err:
                    logger.debug(f"SSE 关闭 BilibiliService 失败: {close_err}")
                await queue.put(None)  # 终止信号

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


@router.post("/operations/{operation_id}/cancel")
async def cancel_operation(
    operation_id: str,
    session_id: str = Query(..., description="会话ID"),
):
    """取消单视频导出或入库操作。"""
    session = await get_session(session_id, platform="bilibili")
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    operation = active_operations.get(operation_id)
    if operation and operation[0] == session_id:
        operation[1].set()
        logger.info(f"收到操作取消请求 [{operation_id}]")
        return {"message": "取消请求已发送"}
    return {"message": "操作已结束或不存在"}


# ---------------------------------------------------------------------------
#  RAG 入库管理：跨平台视频列表 + 视频级批量入库（带进度）
# ---------------------------------------------------------------------------

@router.get("/videos/list", response_model=List[VideoListItem])
async def list_all_videos(
    session_id: str = Query(..., description="B站会话ID"),
    douyin_session_id: Optional[str] = Query(None, description="抖音会话ID"),
    platform: Optional[str] = Query(None, description="平台过滤：bilibili / douyin"),
    status: Optional[str] = Query(None, description="状态过滤：processed / pending / failed"),
    tag: Optional[str] = Query(None, description="标签筛选"),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户所有视频及入库状态（跨平台）。

    供 RAG 入库管理页面展示，支持按平台和入库状态筛选。
    """
    results: list[VideoListItem] = []

    # ---- B站视频 ----
    if platform in (None, "bilibili"):
        session = await get_session(session_id, platform="bilibili")
        if session:
            target_session_ids = await _get_session_ids_for_user(db, session_id)
            rows = (await db.execute(
                select(
                    VideoCache.bvid,
                    VideoCache.title,
                    VideoCache.owner_name,
                    VideoCache.duration,
                    VideoCache.is_processed,
                    VideoCache.process_error,
                    VideoCache.tags,
                    FavoriteFolder.id.label("folder_id"),
                    FavoriteFolder.title.label("folder_title"),
                )
                .join(FavoriteVideo, FavoriteVideo.bvid == VideoCache.bvid)
                .join(FavoriteFolder, FavoriteFolder.id == FavoriteVideo.folder_id)
                .where(
                    # 兼容旧数据：platform 可能为 NULL（v0.3.14 之前的版本未显式设置）
                    # 这些旧数据默认属于 bilibili，下次同步时会被 _upsert_video_cache 补全
                    or_(VideoCache.platform == "bilibili", VideoCache.platform.is_(None)),
                    FavoriteFolder.session_id.in_(target_session_ids),
                    *([VideoCache.tags.contains(tag)] if tag else []),
                )
                .order_by(VideoCache.created_at.desc())
            )).all()

            for row in rows:
                # 前端逻辑：is_processed 优先于 process_error
                if status == "failed":
                    # 只有 is_processed=False 且有 process_error 的才算失败
                    if row.is_processed or not row.process_error:
                        continue
                elif status == "processed":
                    if not row.is_processed:
                        continue
                elif status == "pending":
                    if row.is_processed or row.process_error:
                        continue
                results.append(VideoListItem(
                    bvid=row.bvid,
                    platform="bilibili",
                    title=row.title or row.bvid,
                    author=row.owner_name or "",
                    duration=row.duration or 0,
                    is_processed=bool(row.is_processed),
                    process_error=row.process_error,
                    folder_id=row.folder_id,
                    folder_title=row.folder_title or "",
                    tags=_parse_tags(row.tags),
                ))

    # ---- 抖音视频 ----
    if platform in (None, "douyin") and douyin_session_id:
        user_scope = douyin_session_id
        rows = (await db.execute(
            select(
                VideoCache.bvid,
                VideoCache.title,
                VideoCache.owner_name,
                VideoCache.duration,
                VideoCache.is_processed,
                VideoCache.process_error,
                VideoCache.tags,
                FavoriteFolder.id.label("folder_id"),
                FavoriteFolder.title.label("folder_title"),
            )
            .join(FavoriteVideo, FavoriteVideo.bvid == VideoCache.bvid)
            .join(FavoriteFolder, FavoriteFolder.id == FavoriteVideo.folder_id)
            .where(
                VideoCache.platform == "douyin",
                FavoriteFolder.session_id.like(f"douyin-{user_scope}-%"),
                *([VideoCache.tags.contains(tag)] if tag else []),
            )
            .order_by(VideoCache.created_at.desc())
        )).all()

        for row in rows:
            # 前端逻辑：is_processed 优先于 process_error
            if status == "failed":
                # 只有 is_processed=False 且有 process_error 的才算失败
                if row.is_processed or not row.process_error:
                    continue
            elif status == "processed":
                if not row.is_processed:
                    continue
            elif status == "pending":
                if row.is_processed or row.process_error:
                    continue
            results.append(VideoListItem(
                bvid=row.bvid,
                platform="douyin",
                title=row.title or row.bvid,
                author=row.owner_name or "",
                duration=row.duration or 0,
                is_processed=bool(row.is_processed),
                process_error=row.process_error,
                folder_id=row.folder_id,
                folder_title=row.folder_title or "",
                tags=_parse_tags(row.tags),
            ))

    return results


@router.post("/ingest-videos")
async def ingest_videos(
    payload: VideoIngestRequest,
    background_tasks: BackgroundTasks,
    session_id: str = Query(..., description="B站会话ID"),
    douyin_session_id: Optional[str] = Query(None, description="抖音会话ID"),
):
    """视频级批量入库（后台任务，带进度轮询）。

    接收跨平台视频列表，按 platform 路由到对应入库逻辑，
    通过 task_tracker_service 跟踪进度，前端通过 /knowledge/build/status/{task_id} 轮询。
    """
    set_operation_type("user_action")
    if not payload.videos:
        raise HTTPException(status_code=400, detail="视频列表不能为空")

    # 校验至少有一个平台的会话有效
    bili_session = await get_session(session_id, platform="bilibili")
    has_bili = any(v.platform == "bilibili" for v in payload.videos)
    if has_bili and not bili_session:
        raise HTTPException(status_code=401, detail="B站会话已过期，请重新登录")

    total = len(payload.videos)
    # 去重键：同一 session 同时只能有一个 ingest_videos 任务
    dedup_video_id = f"ingest:{session_id}"
    metadata = {
        "platform": "bilibili",
        "session_id": session_id,
        "total_videos": total,
        "processed_videos": 0,
        "total_folders": None,
        "processed_folders": None,
        "current_folder_id": None,
        "current_folder_title": None,
        "current_video_title": None,
        "message": "",
        "succeeded": 0,
        "failed": 0,
        # 保存原始参数，供 auto_retry 重启时恢复
        "videos": [
            {"bvid": v.bvid, "platform": v.platform, "tags": v.tags}
            for v in payload.videos
        ],
        "douyin_session_id": douyin_session_id,
    }
    task_id = await task_tracker.create_task_if_not_exists(
        task_type="ingest_videos",
        video_id=dedup_video_id,
        metadata=metadata,
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有批量入库任务正在运行，请等待完成后再试")

    background_tasks.add_task(
        _ingest_videos_task,
        task_id,
        session_id,
        bili_session,
        douyin_session_id,
        payload.videos,
    )

    return {"task_id": task_id, "message": "批量入库任务已启动"}


async def _ingest_videos_task(
    task_id: str,
    bili_session_id: str,
    bili_session: Optional[dict],
    douyin_session_id: Optional[str],
    videos: List[VideoIngestItem],
):
    """视频级批量入库后台任务：按平台路由，逐视频处理并更新进度。"""
    set_operation_type("background_task")
    total = len(videos)
    succeeded = 0
    failed = 0
    # 批量入库后台任务无显式取消事件，传入 None 表示不可取消
    cancel_check = None

    async def _report_progress(step: str, video_title: Optional[str] = None) -> None:
        processed = succeeded + failed
        await task_tracker.update_task(
            task_id,
            step=step,
            progress=int(processed / total * 100) if total else 100,
            metadata={
                "processed_videos": processed,
                "succeeded": succeeded,
                "failed": failed,
                "current_video_title": video_title,
            },
        )

    try:
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.RUNNING,
            step="开始批量入库...",
        )

        bili_videos = [v for v in videos if v.platform == "bilibili"]
        douyin_videos = [v for v in videos if v.platform == "douyin"]

        # ---- B站视频入库 ----
        if bili_videos and bili_session:
            bili = None
            bili_counted = 0
            try:
                cookies = bili_session.get("cookies", {})
                bili = BilibiliService(
                    sessdata=cookies.get("SESSDATA"),
                    bili_jct=cookies.get("bili_jct"),
                    dedeuserid=cookies.get("DedeUserID"),
                )
                asr_service = ASRService()
                content_fetcher = ContentFetcher(bili, asr_service)
                rag_bili = get_rag_service("bilibili")
                for item in bili_videos:
                    await task_tracker.update_task(task_id, step=f"处理: {item.bvid}")
                    try:
                        async with get_db_context() as db:
                            # 查找视频所属收藏夹的 media_id（B站收藏夹ID）
                            folder_row = (await db.execute(
                                select(FavoriteFolder.media_id)
                                .join(FavoriteVideo, FavoriteVideo.folder_id == FavoriteFolder.id)
                                .where(FavoriteVideo.bvid == item.bvid)
                                .limit(1)
                            )).first()
                            if not folder_row:
                                failed += 1
                                logger.warning(f"[RAG管理] B站视频未找到收藏夹关联 [{item.bvid}]")
                            else:
                                media_id = folder_row[0]
                                cache = await _ingest_single_video(
                                    db, bili, rag_bili, content_fetcher,
                                    bili_session_id, media_id, item.bvid,
                                )
                                if item.tags:
                                    cache.tags = json.dumps(item.tags)
                                    await db.commit()
                                succeeded += 1
                                await task_tracker.update_task(
                                    task_id,
                                    metadata={"current_video_title": cache.title},
                                )
                    except HTTPException as he:
                        failed += 1
                        logger.warning(f"[RAG管理] B站视频入库被拒 [{item.bvid}]: {he.detail}")
                    except Exception as e:
                        failed += 1
                        logger.error(f"[RAG管理] B站视频入库失败 [{item.bvid}]: {e}")

                    bili_counted += 1
                    await _report_progress(f"处理: {item.bvid}")
            except Exception as e:
                logger.error(f"[RAG管理] B站视频入库整体失败: {e}", exc_info=True)
                failed += len(bili_videos) - bili_counted
            finally:
                if bili is not None:
                    try:
                        await bili.close()
                    except Exception as close_err:
                        logger.warning(f"[RAG管理] 关闭 BilibiliService 失败: {close_err}")

        # ---- 抖音视频入库 ----
        if douyin_videos:
            from app.services.platform.douyin import DouyinPlatformService
            douyin = None
            douyin_counted = 0
            try:
                rag_douyin = get_rag_service("douyin")
                douyin = DouyinPlatformService()
                for item in douyin_videos:
                    await task_tracker.update_task(task_id, step=f"处理: {item.bvid}")
                    cache_title = item.bvid
                    try:
                        async with get_db_context() as db:
                            cache = await db.scalar(
                                select(VideoCache).where(
                                    VideoCache.bvid == item.bvid,
                                    VideoCache.platform == "douyin",
                                )
                            )
                            if not cache:
                                failed += 1
                                logger.warning(f"[RAG管理] 抖音视频缓存不存在 [{item.bvid}]")
                            else:
                                if item.tags:
                                    cache.tags = json.dumps(item.tags)
                                cache_title = cache.title or item.bvid
                                await task_tracker.update_task(
                                    task_id,
                                    metadata={"current_video_title": cache_title},
                                )
                                content = await douyin.fetch_content(item.bvid, ASRService(cancel_check=cancel_check))
                                if content:
                                    cache.content = content.content
                                    cache.content_source = content.source.value
                                    cache.outline_json = content.outline
                                    try:
                                        await asyncio.to_thread(rag_douyin.delete_video, item.bvid)
                                    except Exception:
                                        pass
                                    chunks = await asyncio.to_thread(rag_douyin.add_video_content, content)
                                    if chunks > 0:
                                        _set_cache_processing_result(cache)
                                        succeeded += 1
                                    else:
                                        cache.is_processed = False
                                        cache.process_error = "未生成可写入的向量文档"
                                        failed += 1
                                else:
                                    cache.is_processed = False
                                    cache.process_error = "无法获取视频内容"
                                    failed += 1
                                await db.commit()
                    except Exception as e:
                        failed += 1
                        logger.error(f"[RAG管理] 抖音视频入库失败 [{item.bvid}]: {e}")
                        # 兜底清理：add_video_content 可能已写入部分向量，
                        # 删除避免在 douyin_videos 集合留下孤儿（与 B 站 _ingest_single_video 对齐）
                        try:
                            await asyncio.to_thread(rag_douyin.delete_video, item.bvid)
                        except Exception as cleanup_err:
                            logger.error(f"[douyin {item.bvid}] 清理孤儿向量失败: {cleanup_err}")
                        # 标记缓存为未处理，避免 cache 状态与向量库不一致
                        try:
                            async with get_db_context() as cleanup_db:
                                cleanup_cache = await cleanup_db.scalar(
                                    select(VideoCache).where(
                                        VideoCache.bvid == item.bvid,
                                        VideoCache.platform == "douyin",
                                    )
                                )
                                if cleanup_cache:
                                    cleanup_cache.is_processed = False
                                    cleanup_cache.process_error = str(e)
                                    await cleanup_db.commit()
                        except Exception as commit_err:
                            logger.error(f"[douyin {item.bvid}] 提交失败状态失败: {commit_err}")

                    douyin_counted += 1
                    await _report_progress(f"处理: {item.bvid}")
            except Exception as e:
                logger.error(f"[RAG管理] 抖音视频入库整体失败: {e}", exc_info=True)
                failed += len(douyin_videos) - douyin_counted
            finally:
                if douyin is not None:
                    try:
                        await douyin.close()
                    except Exception as close_err:
                        logger.warning(f"[RAG管理] 关闭 DouyinPlatformService 失败: {close_err}")

        # ---- 完成 ----
        if failed:
            final_message = f"入库完成：成功 {succeeded}，失败 {failed}"
        else:
            final_message = f"入库完成：成功 {succeeded}"
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.SUCCESS,
            progress=100,
            step="完成",
            metadata={
                "current_video_title": None,
                "message": final_message,
            },
        )
        logger.info(f"[RAG管理] 批量入库结束: 成功 {succeeded}，失败 {failed}")

    except Exception as e:
        logger.error(f"[RAG管理] 批量入库任务崩溃: {e}", exc_info=True)
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error_message=f"批量入库任务异常: {e}",
            error_stage="unknown",
            metadata={"message": f"批量入库任务异常: {e}"},
        )


async def _sync_folder_impl(*args, **kwargs):
    """兼容：保持模块级别函数存在。实际逻辑与 _sync_folder 相同。"""
    return await _sync_folder(*args, **kwargs)


# ---------------------------------------------------------------------------
#  本地文件上传入库：走现有 ASR→切片→向量 流程
# ---------------------------------------------------------------------------

@router.post("/local/upload")
async def upload_local_file(file: UploadFile = File(...)):
    """上传本地音频/视频文件并入库（后台 ASR→切片→向量化）。

    - 文件类型白名单校验
    - 分块写入临时文件，累计字节超限立即删除并返回 413
    - 文件名使用 UUID+扩展名，防止路径遍历
    - 入库流程在后台执行，返回 task_id 供前端轮询进度
    """
    # 1. 文件类型校验
    original_filename = file.filename or "local_file"
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext or '无扩展名'}，允许: {sorted(ALLOWED_EXTENSIONS)}",
        )

    # 2. 准备临时文件路径（UUID 命名，防路径遍历；不使用用户提供的文件名）
    upload_dir = os.path.join("data", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    stored_filename = f"{uuid.uuid4().hex}{ext}"
    tmp_path = os.path.join(upload_dir, stored_filename)

    # 3. 分块写入 + running byte count + size limit 校验
    total_bytes = 0
    try:
        async with aiofiles.open(tmp_path, "wb") as f:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_SIZE:
                    await f.close()
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件过大，最大允许 {MAX_UPLOAD_SIZE // (1024 * 1024)} MB",
                    )
                await f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        # 写入异常时清理临时文件
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        logger.error(f"上传文件写入失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件保存失败: {e}")

    # 4. 创建任务（去重）并启动后台入库
    dedup_video_id = f"local_upload:{stored_filename}"
    metadata = {
        "platform": "local",
        "filename": original_filename,
        "stored_filename": stored_filename,
        "total_bytes": total_bytes,
        "message": "",
    }
    task_id = await task_tracker.create_task_if_not_exists(
        task_type="local_upload",
        video_id=dedup_video_id,
        metadata=metadata,
    )
    if task_id is None:
        # 已有相同任务在跑，清理临时文件
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise HTTPException(
            status_code=409,
            detail="已有相同的上传入库任务正在运行，请等待完成后再试",
        )

    # 复用 douyin_auth 的后台任务管理，避免被 GC 中断
    from app.routers.douyin_auth import _spawn_background_task
    _spawn_background_task(
        _local_upload_ingest_task(task_id, tmp_path, original_filename)
    )

    return {
        "task_id": task_id,
        "filename": original_filename,
        "message": "已开始入库",
    }


async def _local_upload_ingest_task(
    task_id: str,
    file_path: str,
    original_filename: str,
):
    """本地文件上传入库后台任务：调用 ingest_local_audio_file 并更新进度。

    临时文件在 finally 中删除，无论成功或失败都不残留。

    断点续传：会创建一条 IngestTask（platform=local）记录入库阶段，
    重启后由 lifespan 恢复逻辑调用 resume_ingest_task 续传。注意：恢复
    时需要临时文件仍存在，因此本任务完成/失败后才删除临时文件——崩溃
    场景下临时文件会保留，重启后 resume_ingest_task 可继续处理。
    """
    set_operation_type("background_task")
    from app.services.data_syncer import ingest_local_audio_file
    from app.services import ingest_task_store

    # 创建 IngestTask 持久化记录（用于断点续传）
    ingest_task_id = None
    try:
        async with get_db_context() as db:
            task = await ingest_task_store.create_task(
                db,
                video_id=f"local_upload:{task_id}",
                platform="local",
                payload={
                    "file_path": file_path,
                    "original_filename": original_filename,
                    "tracker_task_id": task_id,
                },
            )
            await db.commit()
            ingest_task_id = task.id
    except Exception as e:
        logger.warning(f"[local_upload] 创建 IngestTask 失败（不影响入库）: {e}")

    try:
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.RUNNING,
            step="ASR 转写中...",
            metadata={"message": "开始转写"},
        )
        result = await ingest_local_audio_file(
            file_path, original_filename, ingest_task_id=ingest_task_id
        )
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.SUCCESS,
            progress=100,
            step="完成",
            metadata={
                "message": f"入库完成：生成 {result['chunks']} 个片段",
                "bvid": result["bvid"],
                "chunks": result["chunks"],
                "content_length": result["content_length"],
            },
        )
        logger.info(
            f"本地文件上传入库成功: {original_filename}, bvid={result['bvid']}, chunks={result['chunks']}"
        )
    except Exception as e:
        logger.error(f"本地文件上传入库失败 [{original_filename}]: {e}")
        await task_tracker.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error_message=str(e),
            error_stage="unknown",
            metadata={"message": f"入库失败: {e}"},
        )
    finally:
        # 入库完成后删除临时文件
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.warning(f"清理上传临时文件失败 [{file_path}]: {e}")
