"""
Bilibili RAG 知识库系统

认证路由 - 处理 B站登录
"""
import asyncio
from fastapi import APIRouter, HTTPException, Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from app.database import get_db, get_db_context
from app.models import QRCodeResponse, LoginStatusResponse, UserSession as UserSessionModel, Platform
from app.services.bilibili import BilibiliService
from app.services.crypto import encrypt_secret, decrypt_secret
from app.services.tracing import TraceContext, trace_logger
import uuid

router = APIRouter(prefix="/auth", tags=["认证"])


async def startup():
    """启动时初始化浏览器池"""
    try:
        from app.services.browser_pool import browser_pool
        await browser_pool.initialize()
        logger.info("浏览器池已初始化（auth startup）")
    except Exception as e:
        logger.warning(f"浏览器池初始化失败（将在首次使用时重试）: {e}")


async def shutdown():
    """关闭时清理浏览器池"""
    try:
        from app.services.browser_pool import browser_pool
        await browser_pool.close()
        logger.info("浏览器池已关闭（auth shutdown）")
    except Exception as e:
        logger.debug(f"浏览器池关闭异常: {e}")

# 临时存储登录会话（生产环境应使用 Redis）
login_sessions = {}
# 会话级锁：避免多协程并发 miss 时重复查 DB 与覆盖
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    """获取指定 session_id 的锁，避免并发重建。"""
    async with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
        return lock


@router.get("/qrcode", response_model=QRCodeResponse)
async def generate_qrcode():
    """
    生成登录二维码

    返回二维码 key 和 base64 编码的二维码图片
    """
    trace_ctx = TraceContext(step="bili_qrcode_generate")
    trace_ctx.__enter__()
    trace_logger.info("开始生成 B 站登录二维码")
    bili = BilibiliService()
    try:
        result = await bili.generate_qrcode()

        # 存储会话
        login_sessions[result["qrcode_key"]] = {
            "status": "waiting"
        }

        trace_logger.info(f"二维码生成成功: qrcode_key={result['qrcode_key']}")
        return QRCodeResponse(
            qrcode_key=result["qrcode_key"],
            qrcode_url=result["qrcode_url"],
            qrcode_image_base64=result["qrcode_image_base64"]
        )

    except Exception as e:
        trace_logger.error(f"生成二维码失败: {e}")
        logger.error(f"生成二维码失败: {e}")
        raise HTTPException(status_code=500, detail=f"生成二维码失败: {str(e)}")
    finally:
        trace_ctx.__exit__(None, None, None)
        # 异常路径也保证 httpx 连接池释放
        try:
            await bili.close()
        except Exception as close_err:
            logger.debug(f"close bili in generate_qrcode 失败: {close_err}")


@router.get("/qrcode/poll/{qrcode_key}", response_model=LoginStatusResponse)
async def poll_qrcode_status(qrcode_key: str, db: AsyncSession = Depends(get_db)):
    """
    轮询二维码登录状态
    """
    trace_ctx = TraceContext(step=f"bili_qrcode_poll:{qrcode_key}")
    trace_ctx.__enter__()
    trace_logger.info(f"开始轮询二维码状态: qrcode_key={qrcode_key}")
    bili = BilibiliService()
    bili_auth = None
    try:
        result = await bili.poll_qrcode_status(qrcode_key)

        response = LoginStatusResponse(
            status=result["status"],
            message=result["message"]
        )
        trace_logger.info(f"二维码状态: qrcode_key={qrcode_key}, status={result['status']}")

        # 登录成功
        if result["status"] == "confirmed":
            cookies = result.get("cookies", {})

            # 创建会话
            session_id = str(uuid.uuid4())

            # 获取用户信息
            bili_auth = BilibiliService(
                sessdata=cookies.get("SESSDATA"),
                bili_jct=cookies.get("bili_jct"),
                dedeuserid=cookies.get("DedeUserID")
            )

            user_info_dict = {}
            try:
                user_info = await bili_auth.get_user_info()

                mid = int(user_info.get("mid") or cookies.get("DedeUserID"))

                user_info_dict = {
                    "mid": mid,
                    "uname": user_info.get("uname"),
                    "face": user_info.get("face"),
                    "level": user_info.get("level_info", {}).get("current_level")
                }

                # 持久化到数据库
                db_session = UserSessionModel(
                    session_id=session_id,
                    platform=Platform.BILIBILI,
                    bili_mid=mid,
                    bili_uname=user_info.get("uname"),
                    bili_face=user_info.get("face"),
                    user_id=str(mid),
                    username=user_info.get("uname"),
                    avatar_url=user_info.get("face"),
                    sessdata=encrypt_secret(cookies.get("SESSDATA")),
                    bili_jct=encrypt_secret(cookies.get("bili_jct")),
                    dedeuserid=encrypt_secret(str(cookies.get("DedeUserID"))),
                    is_valid=True
                )
                db.add(db_session)
                await db.commit()

                response.user_info = user_info_dict
                trace_logger.info(f"登录成功持久化: session_id={session_id}, mid={mid}")

            except Exception as e:
                trace_logger.warning(f"获取用户信息失败: {e}")
                logger.warning(f"获取用户信息失败: {e}")
                response.user_info = {
                    "mid": cookies.get("DedeUserID"),
                    "uname": "未知用户"
                }

            # 内存缓存：仅在 DB 持久化成功（或 user_info 获取失败但 cookie 有效）后写入
            # 注意：若上面 db.commit() 抛错，整个 except 已捕获并降级为"未知用户"，
            # 但 DB 实际无该 session 记录。为避免攻击者拿到无法失效的 session_id，
            # 此处校验 DB 中确实存在该记录才写入内存缓存。
            try:
                verify = await db.execute(
                    select(UserSessionModel).where(
                        UserSessionModel.session_id == session_id,
                        UserSessionModel.platform == Platform.BILIBILI,
                    )
                )
                if verify.scalar_one_or_none() is None:
                    trace_logger.error(f"会话 {session_id} 未持久化，拒绝写入内存缓存")
                    logger.error(f"会话 {session_id} 未持久化，拒绝写入内存缓存")
                    raise HTTPException(status_code=500, detail="登录会话持久化失败")
            except HTTPException:
                raise
            except Exception as verify_err:
                trace_logger.error(f"校验会话持久化失败: {verify_err}")
                logger.error(f"校验会话持久化失败: {verify_err}")
                raise HTTPException(status_code=500, detail="登录会话校验失败")

            login_sessions[session_id] = {
                "cookies": cookies,
                "user_info": user_info_dict,
                "refresh_token": result.get("refresh_token"),
                "platform": Platform.BILIBILI,
            }

            response.session_id = session_id

            # 清理旧的 qrcode_key
            login_sessions.pop(qrcode_key, None)

        return response

    except HTTPException:
        raise
    except Exception as e:
        trace_logger.error(f"轮询二维码状态失败: {e}")
        logger.error(f"轮询二维码状态失败: {e}")
        raise HTTPException(status_code=500, detail=f"轮询失败: {str(e)}")
    finally:
        trace_ctx.__exit__(None, None, None)
        # 异常路径也保证 httpx 连接池释放
        for client in (bili, bili_auth):
            if client is None:
                continue
            try:
                await client.close()
            except Exception as close_err:
                logger.debug(f"close bili in poll_qrcode 失败: {close_err}")


@router.get("/session/{session_id}")
async def get_session_info(session_id: str, platform: Optional[str] = None):
    """
    获取会话信息。platform 指定时只查找指定平台的会话。
    """
    trace_ctx = TraceContext(step=f"get_session_info:{session_id}")
    trace_ctx.__enter__()
    trace_logger.info(f"查询会话信息: session_id={session_id}, platform={platform}")
    try:
        session = login_sessions.get(session_id)
        if not session:
            async with get_db_context() as db:
                stmt = select(UserSessionModel).where(UserSessionModel.session_id == session_id)
                if platform:
                    stmt = stmt.where(UserSessionModel.platform == platform)
                result = await db.execute(stmt)
                db_session = result.scalar_one_or_none()
            if not db_session or not db_session.is_valid:
                trace_logger.warning(f"会话不存在或已过期: session_id={session_id}")
                raise HTTPException(status_code=404, detail="会话不存在或已过期")
            session = _build_session_dict(db_session)
            login_sessions[session_id] = session

        trace_logger.info(f"会话查询成功: session_id={session_id}")
        return {"valid": True, "user_info": session.get("user_info")}
    finally:
        trace_ctx.__exit__(None, None, None)


def _build_session_dict(db_session: UserSessionModel) -> dict:
    """从 DB 会话记录构建内存会话字典。

    同时支持 B站和抖音字段，保留 cookies/user_info 结构以向后兼容。
    """
    return {
        "session_id": db_session.session_id,
        "platform": db_session.platform,
        "is_valid": db_session.is_valid,
        # B站字段（cookies 结构向后兼容）
        "cookies": {
            "SESSDATA": decrypt_secret(db_session.sessdata) if db_session.sessdata else None,
            "bili_jct": decrypt_secret(db_session.bili_jct) if db_session.bili_jct else None,
            "DedeUserID": decrypt_secret(db_session.dedeuserid) if db_session.dedeuserid else None,
        },
        "user_info": {
            "mid": db_session.bili_mid,
            "uname": db_session.bili_uname,
            "face": db_session.bili_face,
        },
        "bili_mid": db_session.bili_mid,
        "bili_uname": db_session.bili_uname,
        "bili_face": db_session.bili_face,
        # 抖音字段
        "douyin_cookie": decrypt_secret(db_session.douyin_cookie) if db_session.douyin_cookie else None,
        "douyin_uid": db_session.douyin_uid,
    }


@router.delete("/session/{session_id}")
async def logout(session_id: str, platform: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """
    退出登录：同时清理内存缓存和 DB 会话状态。
    platform 指定时只登出指定平台的会话。
    """
    login_sessions.pop(session_id, None)
    _session_locks.pop(session_id, None)

    try:
        stmt = select(UserSessionModel).where(UserSessionModel.session_id == session_id)
        if platform:
            stmt = stmt.where(UserSessionModel.platform == platform)
        result = await db.execute(stmt)
        db_session = result.scalar_one_or_none()
        if db_session:
            db_session.is_valid = False
            await db.commit()
    except Exception as e:
        logger.error(f"退出登录时清理 DB 会话失败 [{session_id}]: {e}")
        await db.rollback()
        return {"message": "已退出本地登录，但后端会话清理失败，建议稍后重试"}

    return {"message": "已退出登录"}


async def get_session(session_id: str, platform: Optional[str] = None) -> dict:
    """
    获取会话信息（内部使用）。并发安全：同一 session_id 的并发请求只查一次 DB。
    platform 指定时只查找指定平台的会话，确保平台隔离。
    """
    session = login_sessions.get(session_id)
    if session:
        if platform and session.get("platform") and session["platform"] != platform:
            logger.warning(f"会话 {session_id} 平台不匹配: 期望 {platform}, 实际 {session.get('platform')}")
            return None
        return session

    lock = await _get_session_lock(session_id)
    async with lock:
        session = login_sessions.get(session_id)
        if session:
            if platform and session.get("platform") and session["platform"] != platform:
                logger.warning(f"会话 {session_id} 平台不匹配: 期望 {platform}, 实际 {session.get('platform')}")
                return None
            return session

        async with get_db_context() as db:
            stmt = select(UserSessionModel).where(UserSessionModel.session_id == session_id)
            if platform:
                stmt = stmt.where(UserSessionModel.platform == platform)
            result = await db.execute(stmt)
            db_session = result.scalar_one_or_none()
            if not db_session or not db_session.is_valid:
                return None
            session = _build_session_dict(db_session)

        if session:
            login_sessions[session_id] = session
        return session
