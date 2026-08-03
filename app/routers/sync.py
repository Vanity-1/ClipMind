"""数据同步校验路由 - ChromaDB 与 SQLite 数据一致性管理"""

from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from typing import List, Optional

from app.database import async_session_factory
from app.models import utcnow
from app.services.data_syncer import DataSyncer, SyncStatus
from app.routers.knowledge import get_rag_service
from app.services.tracing import TraceContext, trace_logger

router = APIRouter(prefix="/sync", tags=["数据同步"])

_last_report: Optional[dict] = None
_last_report_time: Optional[datetime] = None


def _get_syncer(platform: str = "bilibili") -> DataSyncer:
    rag = get_rag_service(platform)
    return DataSyncer(async_session_factory, rag)


@router.post("/check")
async def check_data_consistency(
    since_hours: Optional[int] = Query(
        default=None,
        description="只检查最近N小时内更新的视频，不填则检查全部",
    ),
    platform: str = Query(default="bilibili", description="平台: bilibili / douyin"),
):
    """执行数据一致性检查"""
    trace_ctx = TraceContext(step=f"sync_check:{platform}")
    trace_ctx.__enter__()
    trace_logger.info(f"开始数据一致性检查: platform={platform}, since_hours={since_hours}")
    global _last_report, _last_report_time

    try:
        syncer = _get_syncer(platform)
        report = await syncer.check_consistency(since_hours=since_hours)
        result = report.to_dict()
        _last_report = result
        _last_report_time = utcnow()
        trace_logger.info(
            f"数据一致性检查完成: platform={platform}, "
            f"sqlite_count={result.get('sqlite_count')}, "
            f"vector_count={result.get('vector_count')}, "
            f"mismatch={result.get('mismatch_count', 0)}"
        )
        return result
    except Exception as e:
        trace_logger.error(f"数据一致性检查失败: {e}")
        logger.error(f"数据一致性检查失败: {e}")
        raise HTTPException(status_code=500, detail=f"数据一致性检查失败: {e}")
    finally:
        trace_ctx.__exit__(None, None, None)


@router.get("/status")
async def get_sync_status():
    """获取最近的同步检查报告"""
    global _last_report, _last_report_time

    if _last_report is None:
        return {
            "status": "no_report",
            "message": "尚未执行过同步检查",
        }

    result = dict(_last_report)
    result["generated_at"] = _last_report_time.isoformat() if _last_report_time else None
    return result


@router.post("/cleanup")
async def cleanup_orphan_vectors(
    bvids: Optional[List[str]] = None,
    platform: str = Query(default="bilibili", description="平台: bilibili / douyin"),
    auto: bool = Query(default=False, description="自动清理最近报告中的孤儿向量"),
):
    """清理孤儿向量"""
    trace_ctx = TraceContext(step=f"sync_cleanup:{platform}")
    trace_ctx.__enter__()
    trace_logger.info(f"开始清理孤儿向量: platform={platform}, auto={auto}, bvids_count={len(bvids) if bvids else 0}")
    try:
        syncer = _get_syncer(platform)

        if auto and not bvids:
            global _last_report
            if not _last_report:
                raise HTTPException(status_code=400, detail="没有最近的同步报告可参考，请先执行 /sync/check")
            orphan_bvids = [
                d["bvid"] for d in _last_report.get("details", [])
                if d.get("status") == SyncStatus.VECTOR_ORPHAN.value
            ]
            if not orphan_bvids:
                trace_logger.info("没有需要清理的孤儿向量")
                return {"cleaned": 0, "errors": [], "message": "没有需要清理的孤儿向量"}
            bvids = orphan_bvids

        if not bvids:
            raise HTTPException(status_code=400, detail="请提供要清理的 bvid 列表，或使用 auto=true")

        result = await syncer.cleanup_orphan_vectors(bvids)
        trace_logger.info(
            f"清理孤儿向量完成: platform={platform}, cleaned={result.get('cleaned')}, "
            f"errors={len(result.get('errors', []))}"
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        trace_logger.error(f"清理孤儿向量失败: {e}")
        logger.error(f"清理孤儿向量失败: {e}")
        raise HTTPException(status_code=500, detail=f"清理孤儿向量失败: {e}")
    finally:
        trace_ctx.__exit__(None, None, None)