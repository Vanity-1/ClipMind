"""
Bilibili RAG 知识库系统

数据模型定义
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, JSON, UniqueConstraint, Index
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, field_validator
from enum import Enum

Base = declarative_base()


def utcnow() -> datetime:
    """返回当前 UTC 时间（naive datetime）。

    替代 Python 3.12+ 已弃用的 ``datetime.utcnow()``，保持返回 naive
    datetime 以兼容 SQLite 存储格式和现有的 datetime 比较逻辑
    （aware 与 naive 直接比较会抛 TypeError）。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Platform(str, Enum):
    BILIBILI = "bilibili"
    DOUYIN = "douyin"


# ==================== SQLAlchemy 模型 ====================

class VideoCache(Base):
    """视频内容缓存表"""
    __tablename__ = 'video_cache'
    # (platform, bvid) 复合唯一约束：允许同一 bvid 在不同平台下共存，
    # 同时保证单平台内的 bvid 唯一性，避免跨平台数据互相覆盖。
    __table_args__ = (
        UniqueConstraint('platform', 'bvid', name='uq_platform_bvid'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 保留原有的 bvid 单列索引用于快速查找
    bvid = Column(String(32), unique=True, index=True, nullable=False)
    platform = Column(String(20), default='bilibili', index=True)  # bilibili / douyin
    cid = Column(Integer, nullable=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    owner_name = Column(String(100), nullable=True)  # UP主名称
    owner_mid = Column(Integer, nullable=True)  # UP主ID
    
    # 内容
    content = Column(Text, nullable=True)  # 摘要/字幕文本
    content_source = Column(String(20), nullable=True)  # ai_summary / subtitle / basic_info
    outline_json = Column(JSON, nullable=True)  # 分段提纲
    
    # 元信息
    duration = Column(Integer, nullable=True)  # 视频时长（秒）
    pic_url = Column(String(500), nullable=True)  # 封面URL
    
    # 处理状态
    is_processed = Column(Boolean, default=False)  # 是否已处理并加入向量库
    process_error = Column(Text, nullable=True)  # 处理错误信息

    # 标签（JSON 数组字符串，如 '["教程","游戏"]'）
    tags = Column(Text, nullable=True)

    # 重试与错误详情
    retry_count = Column(Integer, default=0)  # 重试次数
    last_error_stage = Column(String(50), nullable=True)  # 失败阶段: download/asr/embedding/vector/network/timeout/not_found/permission/invalid_video
    last_error_detail = Column(Text, nullable=True)  # 详细错误信息
    permanent_failure = Column(Boolean, default=False)  # 是否为永久性错误（不可重试）

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class UserSession(Base):
    """用户会话表 - 按平台隔离"""
    __tablename__ = 'user_sessions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), index=True, nullable=False)
    platform = Column(String(20), nullable=False, default='bilibili', index=True)  # bilibili / douyin
    
    # 通用用户信息
    user_id = Column(String(100), nullable=True)  # 平台用户ID
    username = Column(String(200), nullable=True)
    avatar_url = Column(String(500), nullable=True)
    
    # Cookie 信息（加密存储更安全）
    cookie_data = Column(Text, nullable=True)  # 加密的完整Cookie JSON

    # B站专用字段（兼容旧数据）
    bili_mid = Column(Integer, nullable=True)
    bili_uname = Column(String(100), nullable=True)
    bili_face = Column(String(500), nullable=True)
    sessdata = Column(Text, nullable=True)
    bili_jct = Column(Text, nullable=True)
    dedeuserid = Column(String(50), nullable=True)
    
    # 抖音专用字段（兼容旧数据）
    douyin_cookie = Column(Text, nullable=True)
    douyin_uid = Column(String(50), nullable=True)
    
    # 状态
    is_valid = Column(Boolean, default=True)
    last_active_at = Column(DateTime, default=utcnow)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint('session_id', 'platform', name='uq_session_platform'),
    )


class FavoriteFolder(Base):
    """收藏夹记录表"""
    __tablename__ = 'favorite_folders'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), index=True, nullable=False)
    platform = Column(String(20), default='bilibili', index=True)  # bilibili / douyin

    # B站收藏夹信息  
    media_id = Column(Integer, nullable=False)  # 收藏夹ID
    fid = Column(Integer, nullable=True)  # 原始ID
    title = Column(String(200), nullable=False)
    media_count = Column(Integer, default=0)  # 视频数量
    
    # 状态
    is_selected = Column(Boolean, default=True)  # 是否选中用于知识库
    last_sync_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class FavoriteVideo(Base):
    """收藏夹-视频关联表"""
    __tablename__ = 'favorite_videos'
    __table_args__ = (
        UniqueConstraint('folder_id', 'bvid', name='uq_folder_bvid'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    folder_id = Column(Integer, index=True, nullable=False)
    bvid = Column(String(32), index=True, nullable=False)

    is_selected = Column(Boolean, default=True)

    created_at = Column(DateTime, default=utcnow)


# ==================== Pydantic 模型 (API 用) ====================

class ContentSource(str, Enum):
    """内容来源"""
    AI_SUMMARY = "ai_summary"
    SUBTITLE = "subtitle"
    BASIC_INFO = "basic_info"
    ASR = "asr"


class VideoInfo(BaseModel):
    """视频信息"""
    bvid: str
    cid: Optional[int] = None
    title: str
    description: Optional[str] = None
    owner_name: Optional[str] = None
    owner_mid: Optional[int] = None
    duration: Optional[int] = None
    platform: Optional[str] = None
    pic_url: Optional[str] = None


class VideoContent(BaseModel):
    """视频内容（含摘要）"""
    bvid: str
    title: str
    content: str
    source: ContentSource
    outline: Optional[list] = None
    description: Optional[str] = None
    owner_name: Optional[str] = None
    owner_mid: Optional[int] = None
    duration: Optional[int] = None
    platform: Optional[str] = None


class QRCodeResponse(BaseModel):
    """二维码响应"""
    qrcode_key: str
    qrcode_url: str
    qrcode_image_base64: str


class LoginStatusResponse(BaseModel):
    """登录状态响应"""
    status: str  # waiting / scanned / confirmed / expired
    message: str
    user_info: Optional[dict] = None
    session_id: Optional[str] = None


class FavoriteFolderInfo(BaseModel):
    """收藏夹信息"""
    media_id: int
    title: str
    media_count: int
    is_selected: bool = True
    is_default: Optional[bool] = None
    platform: Optional[str] = None


class ChatRequest(BaseModel):
    """对话请求

    platform 字段语义：
    - "bilibili" / "douyin"：仅检索指定平台
    - None：检索全部平台
    前端历史代码可能传 "all"，这里通过校验器统一归一化为 None，
    避免后端每个判断点都要重复 `!= "all"` 的防御逻辑。

    chat_session_id 字段语义：
    - 关联到 ChatSession 表的主键，用于将本次问答写入历史会话。
    - 不提供时行为不变（向后兼容）。
    """
    question: str
    session_id: Optional[str] = None
    folder_ids: Optional[list[int]] = None  # 指定收藏夹，None 表示全部
    platform: Optional[str] = None
    chat_session_id: Optional[str] = None  # 关联 ChatSession.id，提供则记录问答历史

    @field_validator("platform")
    @classmethod
    def normalize_platform(cls, v: Optional[str]) -> Optional[str]:
        if v == "all":
            return None
        return v


class ChatResponse(BaseModel):
    """对话响应"""
    answer: str
    sources: list[dict]  # 来源视频列表


# ==================== TaskTracker 持久化表 ====================

class TaskRecord(Base):
    """TaskTracker 任务状态持久化表。

    进程崩溃/重启后，TaskTracker 可以从本表恢复任务状态，
    避免纯内存字典导致的任务信息丢失。
    """
    __tablename__ = 'task_records'

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), unique=True, index=True, nullable=False)
    video_id = Column(String(128), index=True, default="")
    status = Column(String(20), index=True, default="pending")
    current_step = Column(String(200), default="")
    progress = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    error_stage = Column(String(50), nullable=True)
    retry_count = Column(Integer, default=0)
    trace_id = Column(String(64), nullable=True)
    platform = Column(String(20), index=True, nullable=True)
    session_id = Column(String(64), index=True, nullable=True)
    task_type = Column(String(50), index=True, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class PendingCleanup(Base):
    """待清理的向量记录。

    当 add_video_content 补偿删除失败时，将待清理的 vector_id 写入本表，
    下次 DataSyncer 启动时优先清理，避免孤儿向量留到下一次周期检查。
    """
    __tablename__ = 'pending_cleanup'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bvid = Column(String(32), index=True, nullable=False)
    platform = Column(String(20), index=True, nullable=True)
    vector_ids_json = Column(JSON, nullable=False)  # ["id1","id2",...]
    reason = Column(String(200), default="compensation_failed")
    created_at = Column(DateTime, default=utcnow)
    cleaned = Column(Boolean, default=False)
    cleaned_at = Column(DateTime, nullable=True)


class ChatCache(Base):
    """问答缓存表。

    用于在用户重复提问时跳过 LLM 调用，直接返回历史答案，
    TTL 由应用层（CHAT_CACHE_TTL_SEC）控制，过期记录惰性删除。
    question_hash 对归一化后的问题做 sha256，作为唯一索引实现幂等写入。
    """
    __tablename__ = 'chat_cache'

    id = Column(Integer, primary_key=True, autoincrement=True)
    # sha256 hex（归一化后的问题），unique index 用于快速查找与幂等 upsert
    question_hash = Column(String(64), unique=True, index=True, nullable=False)
    question = Column(Text, nullable=False)  # 原始问题文本，便于排查
    answer = Column(Text, nullable=False)
    # 检索命中的文档 id 列表（JSON 数组，如 ["BV1xxx","BV2yyy"]）
    retrieved_doc_ids_json = Column(Text, nullable=True)
    llm_provider = Column(String(32), nullable=True)  # 生成答案所用模型名
    created_at = Column(DateTime, default=utcnow, nullable=False)


class SyncCursor(Base):
    """增量同步游标表。

    按 (platform, folder_id) 维度记录每个收藏夹的上次同步状态，用于增量同步：
    - B站：记录 last_sync_at（上次同步的最大 fav_time Unix 时间戳），
      下次同步用 order=mtime 倒序拉取，遇到 fav_time <= last_sync_at 提前 break。
    - 抖音：记录 last_synced_ids_json（已同步 aweme_id 集合的 JSON 数组），
      下次同步对当前收藏夹做集合 diff，仅处理新增 aweme_id。

    首次同步（无对应记录）走全量逻辑，last_sync_at / last_synced_ids_json 均为 None。
    """
    __tablename__ = 'sync_cursor'
    __table_args__ = (
        UniqueConstraint('platform', 'folder_id', name='uq_sync_cursor'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(32), nullable=False)  # bilibili / douyin
    folder_id = Column(String(64), nullable=False)  # 收藏夹ID（B站 media_id / 抖音 folder_title）
    # B站增量同步使用：上次同步的最大 fav_time（Unix 时间戳，秒）
    last_sync_at = Column(Integer, nullable=True)
    # 抖音增量同步使用：已同步的视频ID集合（JSON 数组，如 ["7011xxx","7012yyy"]）
    last_synced_ids_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


class IngestTask(Base):
    """入库任务持久化表。

    记录每个视频的入库阶段（download/transcode/asr/embedding/done）与
    状态（pending/running/done/failed），程序重启后从断点继续未完成的任务。
    payload_json 保存任务参数（url、title、file_path 等），供恢复时重建上下文。
    """
    __tablename__ = "ingest_task"

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(String(64), nullable=False)  # bvid 或 aweme_id
    platform = Column(String(32), nullable=False)  # bilibili/douyin/local
    stage = Column(String(32), nullable=False, default="download")  # download/transcode/asr/embedding/done
    status = Column(String(16), nullable=False, default="pending")  # pending/running/done/failed
    retry_count = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)  # 任务参数（url, title 等）JSON
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_ingest_task_video_platform", "video_id", "platform"),
    )


class ChatSession(Base):
    """对话会话表。

    用于持久化用户的问答会话，支持新建 / 重命名 / 删除会话，
    以及回看历史对话。一个会话包含多条 ChatMessage。
    """
    __tablename__ = 'chat_session'

    id = Column(String(36), primary_key=True)  # uuid
    title = Column(String(128), nullable=False, default="新会话")
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class ChatMessage(Base):
    """对话消息表。

    每条消息归属一个 ChatSession，按 created_at 升序排列即为对话顺序。
    role 取值为 user / assistant，分别对应用户提问与助手回答。
    """
    __tablename__ = 'chat_message'

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), nullable=False, index=True)
    role = Column(String(16), nullable=False)  # user/assistant
    content = Column(Text, nullable=False)
    # 引用片段 doc ids（JSON 数组字符串，如 ["BV1xxx","BV2yyy"]），仅 assistant 有
    retrieved_doc_ids_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
