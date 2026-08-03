"""
数据库迁移脚本 — 添加唯一约束

处理 SQLite 不支持 ALTER TABLE ADD CONSTRAINT 的限制，
使用重建表策略完成迁移。

可独立运行：python -m app.migration
"""
import sqlite3
import os
import sys
import json
from loguru import logger

from app.config import settings, _DATA_DIR


def _get_db_path() -> str:
    """从 database_url 提取 SQLite 文件路径。"""
    url = settings.database_url
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        return path
    if url.startswith("sqlite+aiosqlite:///"):
        path = url[len("sqlite+aiosqlite:///"):]
        return path
    raise RuntimeError(f"不支持的数据库 URL: {url}")


def _check_duplicates(conn: sqlite3.Connection, table: str, cols: list[str]) -> list[dict]:
    """检查表中指定列的重复数据。"""
    col_list = ", ".join(cols)
    sql = (
        f"SELECT {col_list}, COUNT(*) as cnt "
        f"FROM {table} "
        f"GROUP BY {col_list} "
        f"HAVING cnt > 1"
    )
    cursor = conn.execute(sql)
    rows = cursor.fetchall()
    duplicates = []
    for row in rows:
        dup = dict(zip(cols + ["cnt"], row))
        duplicates.append(dup)
    return duplicates


def _rebuild_favorite_videos(conn: sqlite3.Connection) -> int:
    """重建 favorite_videos 表，添加 (folder_id, bvid) 唯一约束。

    返回被删除的重复记录数。
    """
    logger.info("检查 favorite_videos 表重复数据...")
    dups = _check_duplicates(conn, "favorite_videos", ["folder_id", "bvid"])
    removed_count = 0

    if dups:
        logger.warning(f"发现 {len(dups)} 组重复 (folder_id, bvid) 数据")
        for dup in dups:
            logger.warning(f"  folder_id={dup['folder_id']}, bvid={dup['bvid']}, count={dup['cnt']}")

        conn.execute("PRAGMA foreign_keys = OFF")

        conn.execute(
            "CREATE TABLE favorite_videos_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "folder_id INTEGER NOT NULL,"
            "bvid VARCHAR(32) NOT NULL,"
            "is_selected BOOLEAN DEFAULT 1,"
            "created_at DATETIME,"
            "UNIQUE(folder_id, bvid)"
            ")"
        )

        conn.execute(
            "INSERT INTO favorite_videos_new (id, folder_id, bvid, is_selected, created_at) "
            "SELECT id, folder_id, bvid, is_selected, created_at "
            "FROM favorite_videos "
            "WHERE id IN (SELECT MIN(id) FROM favorite_videos GROUP BY folder_id, bvid)"
        )
        removed_count = conn.total_changes

        conn.execute("DROP TABLE favorite_videos")
        conn.execute("ALTER TABLE favorite_videos_new RENAME TO favorite_videos")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_favorite_videos_folder_bvid "
            "ON favorite_videos (folder_id, bvid)"
        )

        conn.execute("PRAGMA foreign_keys = ON")
        logger.info(f"favorite_videos 表重建完成，去除了 {removed_count} 条重复记录")
    else:
        logger.info("favorite_videos 表无重复数据，直接添加唯一约束")
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_folder_bvid "
                "ON favorite_videos (folder_id, bvid)"
            )
            logger.info("唯一索引 uq_folder_bvid 创建成功")
        except Exception as e:
            logger.warning(f"创建唯一索引失败: {e}")

    return removed_count


def _migrate_user_sessions(conn: sqlite3.Connection) -> int:
    """迁移 user_sessions 表：添加 platform 字段并建立 (session_id, platform) 唯一约束。

    迁移策略：
    1. 检查 platform 列是否已存在，不存在则添加（默认 "bilibili"）
    2. 将已有抖音会话（douyin_cookie 不为空）的 platform 设为 "douyin"
    3. 重建表以添加 UNIQUE(session_id, platform) 约束，去除重复的 (session_id, platform) 记录

    返回被删除的重复记录数。
    """
    logger.info("检查 user_sessions 表 platform 字段...")

    cols = [row[1] for row in conn.execute("PRAGMA table_info(user_sessions)").fetchall()]

    if "platform" not in cols:
        logger.info("添加 platform 列到 user_sessions 表...")
        conn.execute(
            "ALTER TABLE user_sessions ADD COLUMN platform VARCHAR(20) DEFAULT 'bilibili'"
        )
        cols.append("platform")
        logger.info("platform 列已添加")

        # 将已有抖音会话标记为 platform=douyin
        conn.execute(
            "UPDATE user_sessions SET platform = 'douyin' "
            "WHERE douyin_cookie IS NOT NULL AND douyin_cookie != ''"
        )
        logger.info("已将抖音会话标记为 platform=douyin")
    else:
        logger.info("platform 列已存在，更新空值...")
        # 补填空值
        conn.execute(
            "UPDATE user_sessions SET platform = 'bilibili' "
            "WHERE platform IS NULL OR platform = ''"
        )
        # 重新标记抖音会话
        conn.execute(
            "UPDATE user_sessions SET platform = 'douyin' "
            "WHERE (platform IS NULL OR platform = '' OR platform = 'bilibili') "
            "AND douyin_cookie IS NOT NULL AND douyin_cookie != ''"
        )

    logger.info("检查 user_sessions 表 (session_id, platform) 重复数据...")
    dups = _check_duplicates(conn, "user_sessions", ["session_id", "platform"])
    removed_count = 0

    if dups:
        logger.warning(f"发现 {len(dups)} 组重复 (session_id, platform) 数据")
        for dup in dups:
            logger.warning(f"  session_id={dup['session_id']}, platform={dup['platform']}, count={dup['cnt']}")

        conn.execute("PRAGMA foreign_keys = OFF")

        all_cols = cols
        col_defs = ", ".join(all_cols)
        new_col_defs = col_defs + ", UNIQUE(session_id, platform)"

        conn.execute(f"CREATE TABLE user_sessions_new AS SELECT * FROM user_sessions WHERE 0")

        conn.execute(
            f"INSERT INTO user_sessions_new ({col_defs}) "
            f"SELECT {col_defs} FROM user_sessions "
            f"WHERE id IN (SELECT MIN(id) FROM user_sessions GROUP BY session_id, platform)"
        )
        removed_count = conn.total_changes

        conn.execute("DROP TABLE user_sessions")
        conn.execute("ALTER TABLE user_sessions_new RENAME TO user_sessions")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_user_sessions_platform "
            "ON user_sessions (platform)"
        )

        conn.execute("PRAGMA foreign_keys = ON")
        logger.info(f"user_sessions 表重建完成，去除了 {removed_count} 条重复记录")
    else:
        logger.info("user_sessions 表无重复 (session_id, platform) 数据")
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_session_platform "
                "ON user_sessions (session_id, platform)"
            )
            logger.info("唯一索引 uq_session_platform 创建成功")
        except Exception as e:
            logger.warning(f"创建唯一索引失败: {e}")

    return removed_count


def _add_video_cache_unique(conn: sqlite3.Connection) -> int:
    """为 video_cache 表添加 (platform, bvid) 复合唯一约束。

    约束名 uq_platform_bvid 与 SQLAlchemy 模型 __table_args__ 保持一致，
    便于 init_db 中的 CREATE UNIQUE INDEX IF NOT EXISTS 幂等创建。
    返回被删除的重复记录数。
    """
    logger.info("检查 video_cache 表重复数据...")
    dups = _check_duplicates(conn, "video_cache", ["bvid", "platform"])
    removed_count = 0

    if dups:
        logger.warning(f"发现 {len(dups)} 组重复 (bvid, platform) 数据")
        for dup in dups:
            logger.warning(f"  bvid={dup['bvid']}, platform={dup['platform']}, count={dup['cnt']}")

        conn.execute("PRAGMA foreign_keys = OFF")

        conn.execute(
            "CREATE TABLE video_cache_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "bvid VARCHAR(32) NOT NULL,"
            "platform VARCHAR(20) DEFAULT 'bilibili',"
            "cid INTEGER,"
            "title VARCHAR(500) NOT NULL,"
            "description TEXT,"
            "owner_name VARCHAR(100),"
            "owner_mid INTEGER,"
            "content TEXT,"
            "content_source VARCHAR(20),"
            "outline_json JSON,"
            "duration INTEGER,"
            "pic_url VARCHAR(500),"
            "is_processed BOOLEAN DEFAULT 0,"
            "process_error TEXT,"
            "tags TEXT,"
            "retry_count INTEGER DEFAULT 0,"
            "last_error_stage VARCHAR(50),"
            "last_error_detail TEXT,"
            "permanent_failure BOOLEAN DEFAULT 0,"
            "created_at DATETIME,"
            "updated_at DATETIME,"
            "UNIQUE(platform, bvid)"
            ")"
        )

        conn.execute(
            "INSERT INTO video_cache_new "
            "(id, bvid, platform, cid, title, description, owner_name, owner_mid, "
            "content, content_source, outline_json, duration, pic_url, "
            "is_processed, process_error, tags, retry_count, last_error_stage, last_error_detail, permanent_failure, "
            "created_at, updated_at) "
            "SELECT id, bvid, platform, cid, title, description, owner_name, owner_mid, "
            "content, content_source, outline_json, duration, pic_url, "
            "is_processed, process_error, tags, 0, NULL, NULL, 0, "
            "created_at, updated_at "
            "FROM video_cache "
            "WHERE id IN (SELECT MIN(id) FROM video_cache GROUP BY bvid, platform)"
        )
        removed_count = conn.total_changes

        conn.execute("DROP TABLE video_cache")
        conn.execute("ALTER TABLE video_cache_new RENAME TO video_cache")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_video_cache_bvid "
            "ON video_cache (bvid)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_video_cache_platform "
            "ON video_cache (platform)"
        )
        # 显式创建与 models.py __table_args__ 同名的复合唯一索引，
        # 重建后的表内联 UNIQUE(platform, bvid) 已提供唯一性保证，
        # 此处仅用于让 PRAGMA index_list 输出名与 ORM 保持一致。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_bvid "
            "ON video_cache (platform, bvid)"
        )

        conn.execute("PRAGMA foreign_keys = ON")
        logger.info(f"video_cache 表重建完成，去除了 {removed_count} 条重复记录")
    else:
        logger.info("video_cache 表无重复数据，直接添加唯一约束")
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_bvid "
                "ON video_cache (platform, bvid)"
            )
            logger.info("唯一索引 uq_platform_bvid 创建成功")
        except Exception as e:
            logger.warning(f"创建唯一索引失败: {e}")

    return removed_count


def _add_video_cache_retry_fields(conn: sqlite3.Connection) -> int:
    """为 video_cache 表添加重试和错误详情字段。

    如果字段已存在则跳过，采用 ADD COLUMN 方式原地添加。
    """
    logger.info("检查 video_cache 表重试字段...")

    cols = [row[1] for row in conn.execute("PRAGMA table_info(video_cache)").fetchall()]

    new_fields = {
        "retry_count": "INTEGER DEFAULT 0",
        "last_error_stage": "VARCHAR(50)",
        "last_error_detail": "TEXT",
        "permanent_failure": "BOOLEAN DEFAULT 0",
    }

    added_count = 0
    for col_name, col_def in new_fields.items():
        if col_name not in cols:
            try:
                conn.execute(
                    f"ALTER TABLE video_cache ADD COLUMN {col_name} {col_def}"
                )
                logger.info(f"添加 video_cache.{col_name} 成功")
                added_count += 1
            except Exception as e:
                logger.warning(f"添加 video_cache.{col_name} 失败: {e}")
        else:
            logger.info(f"video_cache.{col_name} 已存在，跳过")

    return added_count


def run_migration() -> dict:
    """执行数据库迁移，返回迁移结果。"""
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return {
            "status": "success",
            "message": f"数据库文件不存在: {db_path}，跳过迁移",
            "favorite_videos_duplicates": 0,
            "video_cache_duplicates": 0,
            "video_cache_retry_fields_added": 0,
            "user_sessions_duplicates": 0,
        }

    logger.info(f"开始数据库迁移: {db_path}")
    result = {
        "status": "success",
        "message": "",
        "favorite_videos_duplicates": 0,
        "video_cache_duplicates": 0,
        "video_cache_retry_fields_added": 0,
        "user_sessions_duplicates": 0,
        "warnings": [],
    }

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")

        try:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]

            if "favorite_videos" in tables:
                result["favorite_videos_duplicates"] = _rebuild_favorite_videos(conn)
            else:
                result["warnings"].append("favorite_videos 表不存在，跳过")
                logger.warning("favorite_videos 表不存在，跳过")

            if "video_cache" in tables:
                result["video_cache_duplicates"] = _add_video_cache_unique(conn)
                result["video_cache_retry_fields_added"] = _add_video_cache_retry_fields(conn)
            else:
                result["warnings"].append("video_cache 表不存在，跳过")
                logger.warning("video_cache 表不存在，跳过")

            if "user_sessions" in tables:
                result["user_sessions_duplicates"] = _migrate_user_sessions(conn)
            else:
                result["warnings"].append("user_sessions 表不存在，跳过")
                logger.warning("user_sessions 表不存在，跳过")

            conn.commit()
            logger.info("数据库迁移完成")
        except Exception as e:
            conn.rollback()
            logger.error(f"迁移失败: {e}")
            result["status"] = "failed"
            result["message"] = str(e)
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"连接数据库失败: {e}")
        result["status"] = "failed"
        result["message"] = str(e)

    if result["status"] == "success":
        result["message"] = "迁移成功完成"
        parts = []
        if result["favorite_videos_duplicates"]:
            parts.append(f"favorite_videos 去除 {result['favorite_videos_duplicates']} 条")
        if result["video_cache_duplicates"]:
            parts.append(f"video_cache 去除 {result['video_cache_duplicates']} 条")
        if result["user_sessions_duplicates"]:
            parts.append(f"user_sessions 去除 {result['user_sessions_duplicates']} 条")
        retry_fields_added = result.get("video_cache_retry_fields_added", 0)
        if retry_fields_added:
            parts.append(f"video_cache 添加 {retry_fields_added} 个重试/错误字段")
        if parts:
            result["message"] += "（" + "，".join(parts) + "重复）"

    return result


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    result = run_migration()
    print(json.dumps(result, ensure_ascii=False, indent=2))