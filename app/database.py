"""
Bilibili RAG 知识库系统

数据库管理模块
"""
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from contextlib import asynccontextmanager
from loguru import logger
from app.config import settings, _DATA_DIR
from app.models import Base
import os


# 确保数据目录存在（打包后指向 %APPDATA%/ClipMind/data，由 CLIPMIND_DATA_DIR 控制）
os.makedirs(_DATA_DIR, exist_ok=True)

# 创建异步引擎
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    future=True
)

# 创建异步会话工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


def rebuild_engine() -> None:
    """重建异步引擎和会话工厂。

    在 ``reload_settings`` 后调用，使 ``database_url`` / ``debug`` 等配置变更
    立即生效。旧引擎的连接池会被异步释放（不阻塞当前调用）。

    注意：如果 database_url 指向新文件，新文件所在目录必须已存在（SQLite
    不会自动创建目录）。本函数会确保 SQLite 路径的父目录存在。
    """
    global engine, async_session_factory
    # 函数内重新 import，确保拿到 reload_settings 替换后的最新 settings 引用
    from app.config import settings as _settings

    old_engine = engine

    # 确保 SQLite 文件所在目录存在
    url = _settings.database_url
    if url.startswith("sqlite"):
        path = url.split(":///", 1)[-1] if ":///" in url else ""
        if path and path != ":memory:":
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)

    engine = create_async_engine(
        url,
        echo=_settings.debug,
        future=True,
    )
    async_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # 异步释放旧引擎连接池：在事件循环中调度，不阻塞当前调用。
    # 旧引擎引用仍可能被在飞请求持有，dispose 只是关闭池
    # （不强制断开正在使用的连接，连接归还后丢弃）。
    try:
        asyncio.get_running_loop().create_task(old_engine.dispose())
    except RuntimeError:
        # 无运行中的事件循环（同步上下文），跳过异步 dispose。
        # 旧引擎会被 GC 回收，连接随之释放。
        logger.debug("[DB] 旧引擎 dispose 跳过（无运行中的事件循环）")

    logger.info(f"[DB] 引擎已重建: {url}")


# 索引清单：使用 IF NOT EXISTS 保证幂等。
# 这些索引覆盖关键查询路径：
# - user_sessions.bili_mid：按 B 站 mid 反查 session（get_folder_status 等）
# - favorite_folders.(session_id, media_id)：避免同一用户重复创建收藏夹
# - favorite_videos.(folder_id, bvid)：keyword_search_docs 与 get_video_context 的 join
# - favorite_folders.media_id：单列索引兜底
_INDEX_MIGRATIONS = [
    "CREATE INDEX IF NOT EXISTS ix_user_sessions_bili_mid ON user_sessions (bili_mid)",
    "CREATE INDEX IF NOT EXISTS ix_user_sessions_platform ON user_sessions (platform)",
    "CREATE INDEX IF NOT EXISTS ix_favorite_folders_session_media ON favorite_folders (session_id, media_id)",
    "CREATE INDEX IF NOT EXISTS ix_favorite_folders_media_id ON favorite_folders (media_id)",
    "CREATE INDEX IF NOT EXISTS ix_favorite_videos_folder_bvid ON favorite_videos (folder_id, bvid)",
]


async def init_db():
    """初始化数据库（创建表 + 迁移 + 索引）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migration: add douyin cookie columns if not exists
        try:
            await conn.execute(text("ALTER TABLE user_sessions ADD COLUMN douyin_cookie TEXT"))
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                logger.warning(f"[DB] migration douyin_cookie: {e}")
        try:
            await conn.execute(text("ALTER TABLE user_sessions ADD COLUMN douyin_uid VARCHAR(50)"))
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                logger.warning(f"[DB] migration douyin_uid: {e}")

        try:
            await conn.execute(text("ALTER TABLE user_sessions ADD COLUMN platform VARCHAR(20) DEFAULT 'bilibili'"))
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                logger.warning(f"[DB] migration platform: {e}")

        for col_name, col_def in [
            ("user_id", "VARCHAR(100)"),
            ("username", "VARCHAR(200)"),
            ("avatar_url", "VARCHAR(500)"),
            ("cookie_data", "TEXT"),
            ("last_active_at", "DATETIME"),
            ("updated_at", "DATETIME"),
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE user_sessions ADD COLUMN {col_name} {col_def}"))
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    logger.warning(f"[DB] migration {col_name}: {e}")

        # 回填 platform：标记已有抖音会话
        try:
            result = await conn.execute(text(
                "UPDATE user_sessions SET platform='douyin' "
                "WHERE (platform IS NULL OR platform='bilibili') "
                "AND douyin_cookie IS NOT NULL AND douyin_cookie != ''"
            ))
            if result.rowcount > 0:
                logger.info(f"[DB] 回填 {result.rowcount} 条抖音会话 platform=douyin")
        except Exception as e:
            logger.warning(f"[DB] migration platform backfill: {e}")

        # 启用 SQLite WAL 模式：并发读写时减少表锁竞争
        # 仅对 SQLite 生效，PostgreSQL/MySQL 会因语法不匹配跳过
        if "sqlite" in settings.database_url:
            try:
                await conn.execute(text("PRAGMA journal_mode=WAL"))
                await conn.execute(text("PRAGMA busy_timeout=5000"))
            except Exception as e:
                logger.debug(f"[DB] set WAL mode skipped: {e}")

        # 创建索引（幂等，已存在则跳过）
        for stmt in _INDEX_MIGRATIONS:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                logger.debug(f"[DB] index create skipped: {e}")

        # (platform, bvid) 复合唯一索引：与 models.VideoCache __table_args__ 同名，
        # 用于在已存在的旧库上补齐约束（SQLite 不支持 ALTER TABLE ADD CONSTRAINT）。
        # 建索引前必须先去除重复数据，否则 CREATE UNIQUE INDEX 会失败；
        # 重复数据由 app.migration.run_migration() 负责清理，此处用 try 兜底。
        try:
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_bvid "
                "ON video_cache (platform, bvid)"
            ))
        except Exception as e:
            logger.warning(f"[DB] 创建 uq_platform_bvid 失败（可能存在重复数据，请先运行 migration）: {e}")

        # Data migration: 修复 v0.3.14 之前创建的 VideoCache.platform IS NULL 记录
        # 原因：旧版 _upsert_video_cache 未显式设置 platform，依赖 DB default 但部分环境未生效
        # 影响：/videos/list 的 WHERE platform='bilibili' 会过滤掉这些记录，导致入库管理看不到已入库视频
        try:
            result = await conn.execute(
                text("UPDATE video_cache SET platform='bilibili' WHERE platform IS NULL")
            )
            if result.rowcount > 0:
                logger.info(f"[DB] 修复 {result.rowcount} 条 platform=NULL 的 VideoCache 记录为 bilibili")
        except Exception as e:
            logger.warning(f"[DB] migration platform backfill: {e}")

        # Migration: 为 VideoCache 添加重试和错误详情字段
        _video_cache_new_cols = [
            ("retry_count", "INTEGER DEFAULT 0"),
            ("last_error_stage", "VARCHAR(50)"),
            ("last_error_detail", "TEXT"),
            ("permanent_failure", "BOOLEAN DEFAULT 0"),
            ("tags", "TEXT"),
        ]
        for col_name, col_def in _video_cache_new_cols:
            try:
                await conn.execute(text(f"ALTER TABLE video_cache ADD COLUMN {col_name} {col_def}"))
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    logger.warning(f"[DB] migration video_cache.{col_name}: {e}")


async def get_db() -> AsyncSession:
    """获取数据库会话（用于 FastAPI 依赖注入）"""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            # 路由处理中若抛异常（如 commit 失败），显式 rollback 避免事务残留
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context():
    """获取数据库会话（用于上下文管理器）"""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
