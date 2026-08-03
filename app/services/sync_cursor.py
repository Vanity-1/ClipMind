"""增量同步游标（SyncCursor）读写辅助。

按 (platform, folder_id) 维度记录每个收藏夹的上次同步状态：
- B站：``last_sync_at`` 存上次同步的最大 ``fav_time``（Unix 时间戳，秒），
  下次同步按 ``order=mtime`` 倒序拉取，遇到 ``fav_time <= last_sync_at`` 提前 break。
- 抖音：``last_synced_ids_json`` 存已同步 ``aweme_id`` 集合（JSON 数组），
  下次同步对当前收藏夹做集合 diff，仅处理新增 aweme_id。

首次同步（无游标记录）时 ``get_cursor`` 返回 None，调用方应走全量逻辑。
"""
import json
from typing import Optional, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SyncCursor, utcnow


async def get_cursor(
    db: AsyncSession,
    platform: str,
    folder_id: str,
) -> Optional[SyncCursor]:
    """读取指定 (platform, folder_id) 的同步游标，无记录返回 None。"""
    result = await db.execute(
        select(SyncCursor).where(
            SyncCursor.platform == platform,
            SyncCursor.folder_id == str(folder_id),
        )
    )
    return result.scalar_one_or_none()


def load_synced_ids(cursor: Optional[SyncCursor]) -> set[str]:
    """从游标解析已同步的视频ID集合。

    无游标或字段为空时返回空集合（首次同步语义）。
    """
    if cursor is None or not cursor.last_synced_ids_json:
        return set()
    try:
        data = json.loads(cursor.last_synced_ids_json)
    except (ValueError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x) for x in data if x is not None}


def diff_aweme_ids(
    current_ids: Iterable[str],
    synced_ids: Iterable[str],
) -> set[str]:
    """计算当前收藏夹中不在已同步集合内的新增 aweme_id。

    纯函数，便于单测：传入当前抓取到的 aweme_id 列表与已同步集合，
    返回差集（新增 ID）。
    """
    current_set = {str(x) for x in current_ids if x is not None}
    synced_set = {str(x) for x in synced_ids if x is not None}
    return current_set - synced_set


async def upsert_cursor(
    db: AsyncSession,
    platform: str,
    folder_id: str,
    last_sync_at: Optional[int] = None,
    last_synced_ids: Optional[Iterable[str]] = None,
) -> SyncCursor:
    """upsert 同步游标。

    - ``last_sync_at`` 非 None 时写入该字段（B站用，Unix 时间戳秒）；
      为 None 时保持原值不变（避免误清空）。
    - ``last_synced_ids`` 非 None 时序列化为 JSON 数组写入
      ``last_synced_ids_json``（抖音用）；为 None 时保持原值不变。

    传入空集合 ``set()`` 与传入 None 语义不同：空集合会清空字段（表示
    “当前收藏夹为空”），None 表示不更新该字段。
    """
    cursor = await get_cursor(db, platform, str(folder_id))
    if cursor is None:
        cursor = SyncCursor(
            platform=platform,
            folder_id=str(folder_id),
            last_sync_at=last_sync_at,
            last_synced_ids_json=(
                json.dumps([str(x) for x in last_synced_ids], ensure_ascii=False)
                if last_synced_ids is not None
                else None
            ),
            updated_at=utcnow(),
        )
        db.add(cursor)
    else:
        if last_sync_at is not None:
            cursor.last_sync_at = last_sync_at
        if last_synced_ids is not None:
            cursor.last_synced_ids_json = json.dumps(
                [str(x) for x in last_synced_ids], ensure_ascii=False
            )
        cursor.updated_at = utcnow()
    await db.flush()
    return cursor
