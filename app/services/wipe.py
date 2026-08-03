"""
全量数据清理服务 - 卸载全部内容时调用

清理顺序：
1. ChromaDB 两个 collection 的所有向量
2. DB 表：user_sessions、video_cache、favorite_folders、favorite_videos、task_records、pending_cleanup
3. settings.json 文件
4. logs/ 目录下所有日志文件
5. cookie_key.key 密钥文件

保留：
- models/ 目录（ASR 模型，避免用户重新下载）
"""
import asyncio
from pathlib import Path
from typing import List

from loguru import logger
from sqlalchemy import text

from app.config import _data_dir


async def wipe_all_data(confirm: bool) -> dict:
    """全量清理用户数据，保留 ASR 模型目录。

    Args:
        confirm: 必须为 True 才执行清理

    Returns:
        success: 是否全部清理步骤成功
        wiped: 已清理的项目列表
        preserved: 保留的项目列表
        errors: 清理过程中的错误（非致命，单个失败不影响其他步骤）
    """
    if not confirm:
        return {
            "success": False,
            "message": "需要确认参数 confirm=true 才能执行清理",
        }

    wiped: List[str] = []
    preserved: List[str] = []
    errors: List[str] = []

    data_dir = Path(_data_dir())

    # 1. 清理 ChromaDB 向量
    try:
        await _wipe_chroma()
        wiped.append("chroma")
        logger.warning("[Wipe] ChromaDB 向量已清除")
    except Exception as e:
        errors.append(f"chroma: {e}")
        logger.error(f"[Wipe] ChromaDB 清理失败: {e}")

    # 2. 清理 DB 表
    try:
        await _wipe_db_tables()
        wiped.append("db")
        logger.warning("[Wipe] DB 表已清除")
    except Exception as e:
        errors.append(f"db: {e}")
        logger.error(f"[Wipe] DB 表清理失败: {e}")

    # 3. 删除 settings.json
    try:
        settings_file = data_dir / "settings.json"
        if settings_file.exists():
            settings_file.unlink()
            logger.warning("[Wipe] settings.json 已删除")
        wiped.append("settings")
    except Exception as e:
        errors.append(f"settings: {e}")
        logger.error(f"[Wipe] settings.json 删除失败: {e}")

    # 4. 清理 logs/ 目录下所有日志文件
    try:
        logs_dir = data_dir / "logs"
        if logs_dir.exists():
            for item in logs_dir.iterdir():
                if item.is_file():
                    item.unlink()
            logger.warning("[Wipe] 日志文件已清除")
        wiped.append("logs")
    except Exception as e:
        errors.append(f"logs: {e}")
        logger.error(f"[Wipe] 日志清理失败: {e}")

    # 5. 删除 cookie_key.key
    try:
        key_file = data_dir / "cookie_key.key"
        if key_file.exists():
            key_file.unlink()
            logger.warning("[Wipe] cookie_key.key 已删除")
        wiped.append("cookie_key")
    except Exception as e:
        errors.append(f"cookie_key: {e}")
        logger.error(f"[Wipe] cookie_key.key 删除失败: {e}")

    # 保留 models 目录
    models_dir = data_dir / "models"
    if models_dir.exists():
        preserved.append("models")

    return {
        "success": len(errors) == 0,
        "wiped": wiped,
        "preserved": preserved,
        "errors": errors,
    }


async def _wipe_chroma() -> None:
    """清理 ChromaDB 两个 collection 的所有向量。

    直接使用 chromadb 客户端操作，避免实例化 RAGService（后者需要 LLM API Key）。
    采用「先获取全部 ID，再按 ID 删除」的方式，因为 delete(where={}) 在
    ChromaDB 0.5.x 会抛出 ValueError（空 where 不合法）。
    ChromaDB 0.5.x 自动持久化，无需调用 persist()。
    """
    import chromadb
    from app.config import settings

    client = chromadb.PersistentClient(path=settings.chroma_persist_directory)

    def _delete_collection(name: str) -> None:
        collection = client.get_or_create_collection(name)
        result = collection.get(include=[])
        ids = result.get("ids", [])
        if ids:
            collection.delete(ids=ids)
        logger.info(f"[Wipe] collection {name} 已清除 ({len(ids)} 条向量)")

    for collection_name in ("bilibili_videos", "douyin_videos"):
        try:
            await asyncio.to_thread(_delete_collection, collection_name)
        except Exception as e:
            logger.warning(f"[Wipe] collection {collection_name} 清理失败（可能为空）: {e}")

    # 清理 RAGService 缓存，避免残留指向已清空集合的实例
    try:
        from app.routers.knowledge import _rag_services
        _rag_services.clear()
    except Exception:
        pass


async def _wipe_db_tables() -> None:
    """清理所有 DB 表数据。

    按外键依赖反序删除：pending_cleanup → task_records → favorite_videos
    → favorite_folders → video_cache → user_sessions。
    """
    from app.database import get_db_context

    # 表名硬编码（非用户输入），无 SQL 注入风险
    tables = [
        "pending_cleanup",
        "task_records",
        "favorite_videos",
        "favorite_folders",
        "video_cache",
        "user_sessions",
    ]

    async with get_db_context() as db:
        for table in tables:
            try:
                await db.execute(text(f"DELETE FROM {table}"))
                logger.info(f"[Wipe] 表 {table} 已清空")
            except Exception as e:
                logger.warning(f"[Wipe] 表 {table} 清空失败: {e}")
        await db.commit()

    # 重置 crypto 模块的全局状态，避免清理后内存中残留旧密钥
    try:
        import app.services.crypto as crypto_module
        crypto_module._fernet = None
        crypto_module._init_error = None
        logger.info("[Wipe] crypto 模块状态已重置")
    except Exception:
        pass
