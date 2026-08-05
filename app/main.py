"""
ClipMind 知识库系统

主应用入口 — 桌面应用模式：绑定 127.0.0.1 + 托管前端静态文件
"""
import sys
import re
import asyncio
import os
import shutil
from pathlib import Path

if sys.platform == "win32":
    # 使用 ProactorEventLoop 而非 SelectorEventLoop：
    # SelectorEventLoop 在 Windows 上不支持 asyncio.create_subprocess_exec，
    # 导致 Playwright 浏览器启动失败（NotImplementedError）。
    # ProactorEventLoop 才是 Windows 上支持子进程的正确事件循环。
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# HuggingFace 镜像必须在 import huggingface_hub / faster_whisper 之前设置。
# 默认走 hf-mirror.com（国内镜像），可通过 HF_MIRROR_URL 环境变量覆盖。
# 留空则回退到官方 huggingface.co。settings.json 中 hf_mirror_url 可热改。
_hf_mirror = (os.environ.get("HF_MIRROR_URL") or "https://hf-mirror.com").strip()
if _hf_mirror:
    os.environ.setdefault("HF_ENDPOINT", _hf_mirror.rstrip("/"))

# 禁用 chromadb 匿名遥测（避免 capture() 与新版 opentelemetry 不兼容的告警噪音）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from app.config import settings, ensure_directories
from app.database import init_db, get_db_context, async_session_factory
from app.routers import auth, favorites, knowledge, chat, douyin, douyin_auth, settings as settings_router, sync as sync_router, tasks as tasks_router, system, model_market

# 数据目录（打包后指向 %APPDATA%/ClipMind/data，确保可写）
_DATA_DIR = os.environ.get("CLIPMIND_DATA_DIR", "data")
_LOGS_DIR = os.path.join(_DATA_DIR, "logs")
os.makedirs(_LOGS_DIR, exist_ok=True)


# 日志脱敏：匹配 cookie / SESSDATA / bili_jct / session_id / API keys / JWT / DB URL 等敏感字段
_SENSITIVE_PATTERNS = [
    # 1. B站核心 Cookie 字段
    re.compile(r"(SESSDATA|bili_jct|DedeUserID|sessdata|dedeuserid)(\s*[=:]\s*)([^\s,;'\"]+)", re.IGNORECASE),
    # 2. 通用 cookie 字段
    re.compile(r"(cookie\s*[=:]\s*)([^\s,;'\"]+)", re.IGNORECASE),
    # 3. 抖音 cookie 字段
    re.compile(r"(douyin_cookie\s*[=:]\s*)([^\s,;'\"]+)", re.IGNORECASE),
    # 4. session_id (UUID 格式)
    re.compile(r"(session_id\s*[=:]\s*)([0-9a-fA-F-]{36})", re.IGNORECASE),
    # 5. Bearer Token
    re.compile(r"(Bearer\s+)([A-Za-z0-9\-_.=]+)", re.IGNORECASE),
    # 6. API Key 类 (sk-* / sk-proj-* / *-KEY=sk-*)
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:API_KEY|APIKEY|SECRET_KEY|TOKEN|PASSWORD|PWD|COOKIE_ENCRYPTION_KEY)\s*[=:]\s*)(sk-[A-Za-z0-9_\-]+|sk-proj-[A-Za-z0-9_\-]+)", re.IGNORECASE),
    # 7. 独立 sk-* / sk-proj-* key (直接出现在文本中)
    re.compile(r"\b(sk-[A-Za-z0-9_-]{10,})\b"),
    re.compile(r"\b(sk-proj-[A-Za-z0-9_-]{10,})\b"),
    # 8. JWT Token (eyJ 开头的三段 base64)
    re.compile(r"\b(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"),
    # 9. SQLite DB URL: sqlite:///path → sqlite://***
    re.compile(r"(sqlite(?:\+aiosqlite)?://)/[^\s'\"\)]+", re.IGNORECASE),
    # 10. PostgreSQL/MySQL URL: postgresql://user:pass@host → postgresql://***
    re.compile(r"((?:postgresql|postgres|mysql|mariadb|mongodb|redis)(?:\+[a-z]+)?://)[^\s@]+@[^\s'\"\)]+", re.IGNORECASE),
    # 11. password= / pwd= 通用密码字段
    re.compile(r"((?:password|pwd|passwd|secret)\s*[=:]\s*)([^\s,;'\"]+)", re.IGNORECASE),
    # 12. chroma_persist_directory / CLIPMIND_DATA_DIR 等路径泄露
    re.compile(r"((?:chroma_persist_directory|CLIPMIND_DATA_DIR|data_dir|persist_dir)\s*[=:]\s*)([A-Za-z]:[\\/][^\s'\"]+|/[^\s'\"]{3,})", re.IGNORECASE),
]
_SENSITIVE_REPLACEMENT_KV = r"\1******"
_SENSITIVE_REPLACEMENT_STANDALONE = r"******"
_SENSITIVE_REPLACEMENT_URL = r"\1***"


def _redact_sensitive(message: str) -> str:
    if not isinstance(message, str):
        message = str(message)
    redacted = message
    for idx, pattern in enumerate(_SENSITIVE_PATTERNS):
        # 独立值（无 KV 前缀）：standalone sk-*, standalone sk-proj-*, standalone JWT
        if idx in (6, 7, 8):
            redacted = pattern.sub(_SENSITIVE_REPLACEMENT_STANDALONE, redacted)
        # URL 类：SQLite URL, PostgreSQL/MySQL URL
        elif idx in (9, 10):
            redacted = pattern.sub(_SENSITIVE_REPLACEMENT_URL, redacted)
        else:
            # KV 形式：0 SESSDATA, 1 cookie, 2 douyin_cookie, 3 session_id UUID,
            # 4 Bearer, 5 *API_KEY=sk-*, 11 password/pwd/secret,
            # 12 chroma_persist_directory / data_dir 路径
            redacted = pattern.sub(_SENSITIVE_REPLACEMENT_KV, redacted)
    return redacted


def _redact_filter(record):
    record["message"] = _redact_sensitive(record["message"])
    # 注入 operation_type 到日志记录，便于格式化输出和日志筛选
    from app.services.tracing import get_operation_type
    record["extra"]["operation_type"] = get_operation_type() or "system_internal"
    return True


logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{extra[operation_type]}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG" if settings.debug else "INFO",
    filter=_redact_filter,
)
logger.add(
    os.path.join(_LOGS_DIR, "app.log"),
    rotation="50 MB",      # 50MB 切割
    retention="30 days",   # 保留30天
    compression="zip",     # 压缩轮转日志
    level="INFO",
    encoding="utf-8",
    filter=_redact_filter,
)


def _check_crypto_key_status() -> None:
    """启动时检测 Crypto 密钥配置状态并输出对应日志。

    - 生产环境未配置：ERROR 提示明文存储风险
    - 生产环境已配置：INFO 确认加密存储
    - 开发环境：DEBUG 跳过检查
    """
    from app.config import settings
    if not settings.debug and not settings.cookie_encryption_key:
        logger.error(
            "[Crypto] 生产环境未配置 COOKIE_ENCRYPTION_KEY，cookie 将明文存储！"
            "请在 .env 中配置该变量（可用 Fernet.generate_key().decode() 生成）"
        )
    elif not settings.debug and settings.cookie_encryption_key:
        logger.info("[Crypto] 生产环境已配置 COOKIE_ENCRYPTION_KEY，cookie 将加密存储")
    else:
        logger.debug("[Crypto] 开发环境，Crypto 密钥状态检查跳过")


def _get_central_models_dir() -> str:
    """获取集中模型目录路径。

    基于项目所在盘符（与 data/models 同盘，确保 junction 可用）。
    使用纯英文路径避免 Windows junction 中文编码问题。
    """
    data_dir = os.path.abspath(_DATA_DIR)
    drive = os.path.splitdrive(data_dir)[0]  # 例如 "H:"
    return os.path.join(drive, os.sep, "ClipMindModels")


def _is_symlink_or_junction(path: str) -> bool:
    """检查路径是否是符号链接或 junction。"""
    try:
        if os.path.islink(path):
            return True
    except Exception:
        pass
    try:
        import ctypes
        FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        if attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
            return True
    except Exception:
        pass
    return False


def _ensure_model_symlinks() -> None:
    """将模型目录迁移到集中管理位置，通过 junction 避免重复下载。

    集中目录：项目盘符根目录下的 ClipMindModels/
    策略：
    - 如果 data/models/ 已是 junction，跳过
    - 将模型子目录逐个迁移到集中目录（同盘，使用 junction）
    - 失败时回退到本地目录，不影响功能
    """
    central_dir = _get_central_models_dir()
    local_models = os.path.join(_DATA_DIR, "models")

    # 确保集中目录存在
    os.makedirs(central_dir, exist_ok=True)

    # 本地目录不存在，直接创建 junction
    if not os.path.isdir(local_models):
        _replace_with_junction(local_models, central_dir)
        return

    # 已符号链接/junction，跳过
    if _is_symlink_or_junction(local_models):
        logger.debug("[Model] 模型目录已是 junction，跳过")
        return

    # 迁移模型子目录到集中目录
    migrated = []
    for name in os.listdir(local_models):
        src = os.path.join(local_models, name)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(central_dir, name)
        if os.path.exists(dst):
            # 集中目录已有，删除本地副本
            shutil.rmtree(src, ignore_errors=True)
            migrated.append(name)
            continue
        try:
            shutil.move(src, dst)
            logger.info(f"[Model] 已迁移 {name} 到集中目录")
            migrated.append(name)
        except Exception as e:
            logger.warning(f"[Model] 迁移 {name} 失败: {e}")

    if not migrated:
        logger.debug("[Model] 无模型需要迁移")
        return

    # 检查本地目录是否已清空
    remaining = [n for n in os.listdir(local_models)
                 if not n.startswith(".") and os.path.isdir(os.path.join(local_models, n))]
    if remaining:
        logger.warning(f"[Model] 本地目录仍有未迁移内容: {remaining}，跳过 junction")
        return

    # 用 junction 替换本地目录
    _replace_with_junction(local_models, central_dir)


def _replace_with_junction(link_path: str, target: str) -> None:
    """创建目录 junction（同盘，不需要管理员权限）。"""
    import subprocess

    # 如果 link_path 存在，先备份
    backup = link_path + ".bak"
    if os.path.exists(link_path):
        try:
            os.rename(link_path, backup)
        except Exception as e:
            logger.warning(f"[Model] 无法备份目录: {e}")
            return

    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            logger.info(f"[Model] junction 创建成功: {link_path} -> {target}")
            # 删除备份
            if os.path.exists(backup):
                shutil.rmtree(backup, ignore_errors=True)
        else:
            logger.warning(f"[Model] junction 创建失败: {result.stderr.strip()}")
            # 恢复备份
            if os.path.exists(backup):
                if os.path.exists(link_path):
                    try:
                        os.rmdir(link_path)
                    except Exception:
                        shutil.rmtree(link_path, ignore_errors=True)
                os.rename(backup, link_path)
    except Exception as e:
        logger.warning(f"[Model] junction 创建异常: {e}")
        if os.path.exists(backup):
            os.rename(backup, link_path)


def _log_module_fingerprints() -> None:
    """启动时输出关键模块的源文件 mtime 和行数指纹，便于判断运行代码是否为最新版本。

    解决"代码已修改但运行进程未重启"导致问题反复的问题：
    通过对比日志中的 mtime 与磁盘文件的 mtime，可快速判断是否需要重启。
    """
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    # 关键模块相对路径列表
    modules = [
        "app/routers/douyin_auth.py",
        "app/services/platform/douyin.py",
        "app/services/rag.py",
        "app/routers/settings.py",
        "app/services/crypto.py",
    ]

    # 项目根目录：main.py 位于 app/main.py，所以根目录是 main.py 的父目录的父目录
    root_dir = Path(__file__).parent.parent

    for rel_path in modules:
        abs_path = root_dir / rel_path
        try:
            if not abs_path.exists():
                logger.warning(f"[Startup] module={rel_path} status=not_found")
                continue
            stat = abs_path.stat()
            mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            # 计算行数
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                line_count = sum(1 for _ in f)
            logger.info(
                f"[Startup] module={rel_path} mtime={mtime_iso} lines={line_count}"
            )
        except Exception as e:
            logger.warning(f"[Startup] module={rel_path} status=error: {e}")


async def _reschedule_retry_tasks(retry_infos: list) -> None:
    """重新调度 auto_retry_failed_tasks 创建的重试任务。

    根据 task_type 从 metadata 恢复原始参数，调用对应的任务函数。
    BackgroundTasks 需要请求上下文，无法在 lifespan 中使用，因此直接
    调用任务函数（通过 asyncio.create_task 在后台运行）。

    如果无法恢复原始参数（session_id 缺失、session 过期等），将任务
    标记为 FAILED 并设置 error_message="重试参数缺失，请手动重新发起"。
    """
    from app.services.task_tracker_service import task_tracker
    from app.routers.auth import get_session

    _PARAM_MISSING_MSG = "重试参数缺失，请手动重新发起"

    for info in retry_infos:
        new_task_id = info.new_task_id
        task_type = info.task_type
        metadata = info.metadata or {}
        session_id = metadata.get("session_id")

        try:
            if task_type == "build_knowledge_base":
                from app.routers.knowledge import _build_knowledge_base_task

                folder_ids = metadata.get("folder_ids")
                exclude_bvids = metadata.get("exclude_bvids") or []
                if not session_id or folder_ids is None:
                    logger.warning(
                        f"[ClipMind] 重试任务 {new_task_id} 参数缺失"
                        f"（session_id={session_id}, folder_ids={folder_ids}），标记为失败"
                    )
                    await task_tracker.mark_task_failed(
                        new_task_id, _PARAM_MISSING_MSG, error_stage="config"
                    )
                    continue
                session = await get_session(session_id, platform="bilibili")
                if not session:
                    logger.warning(f"[ClipMind] 重试任务 {new_task_id} 会话已过期，标记为失败")
                    await task_tracker.mark_task_failed(
                        new_task_id, _PARAM_MISSING_MSG, error_stage="config"
                    )
                    continue
                asyncio.create_task(
                    _build_knowledge_base_task(
                        new_task_id, session_id, session,
                        list(folder_ids), list(exclude_bvids),
                    )
                )
                logger.info(f"[ClipMind] 重试任务 {new_task_id} 已重新调度为 build_knowledge_base")

            elif task_type == "ingest_videos":
                from app.routers.knowledge import _ingest_videos_task, VideoIngestItem

                videos_raw = metadata.get("videos")
                douyin_session_id = metadata.get("douyin_session_id")
                if not session_id or not videos_raw:
                    logger.warning(
                        f"[ClipMind] 重试任务 {new_task_id} 参数缺失"
                        f"（session_id={session_id}, videos={bool(videos_raw)}），标记为失败"
                    )
                    await task_tracker.mark_task_failed(
                        new_task_id, _PARAM_MISSING_MSG, error_stage="config"
                    )
                    continue
                bili_session = await get_session(session_id, platform="bilibili")
                videos = [
                    VideoIngestItem(
                        bvid=v["bvid"], platform=v["platform"], tags=v.get("tags")
                    )
                    for v in videos_raw
                ]
                asyncio.create_task(
                    _ingest_videos_task(
                        new_task_id, session_id, bili_session,
                        douyin_session_id, videos,
                    )
                )
                logger.info(f"[ClipMind] 重试任务 {new_task_id} 已重新调度为 ingest_videos")

            else:
                logger.warning(
                    f"[ClipMind] 重试任务 {new_task_id} 未知 task_type={task_type}，标记为失败"
                )
                await task_tracker.mark_task_failed(
                    new_task_id, _PARAM_MISSING_MSG, error_stage="config"
                )
        except Exception as e:
            logger.error(f"[ClipMind] 重试任务 {new_task_id} 重新调度失败: {e}")
            try:
                await task_tracker.mark_task_failed(
                    new_task_id, _PARAM_MISSING_MSG, error_stage="config"
                )
            except Exception:
                pass


async def _resume_ingest_tasks() -> None:
    """恢复断点续传的入库任务（IngestTask）。

    启动时调用：
    1. 查询 status in (pending, running) 的 IngestTask
    2. 将 running 重置为 pending（崩溃时进度未知，统一回退）
    3. 对每个 pending 任务用 _spawn_background_task 调度 resume_ingest_task

    单个任务恢复失败不影响其他任务，整体失败由 lifespan 的 try/except 兜底。
    """
    from app.database import get_db_context
    from app.services import ingest_task_store
    from app.services.data_syncer import resume_ingest_task
    from app.routers.douyin_auth import _spawn_background_task

    async with get_db_context() as db:
        # running → pending：崩溃时 running 任务的进度不可信，统一重置
        reset_count = await ingest_task_store.reset_running_to_pending(db)
        if reset_count:
            logger.info(f"[ClipMind] 入库任务: {reset_count} 个 running 重置为 pending")
        await db.commit()

        pending_tasks = await ingest_task_store.get_pending_tasks(db)

    if not pending_tasks:
        logger.info("[ClipMind] 入库任务: 无待恢复任务")
        return

    logger.info(f"[ClipMind] 入库任务: 恢复 {len(pending_tasks)} 个未完成任务")
    for task in pending_tasks:
        try:
            _spawn_background_task(resume_ingest_task(task))
        except Exception as e:
            logger.warning(
                f"[ClipMind] 入库任务 #{task.id} "
                f"[{task.platform}/{task.video_id}] 调度失败: {e}"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log_module_fingerprints()
    logger.info("[ClipMind] Starting...")
    ensure_directories()
    _ensure_model_symlinks()
    await init_db()
    logger.info("[ClipMind] Database initialized")

    # 检测 Crypto 密钥状态
    try:
        _check_crypto_key_status()
    except Exception as e:
        logger.warning(f"Crypto 密钥状态检测失败: {e}")

    # 初始化 Langfuse（可选，默认关闭）
    try:
        from app.services.langfuse_tracer import initialize as init_langfuse
        from app.config import settings
        init_langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            enabled=settings.langfuse_enabled,
        )
    except Exception as e:
        logger.warning(f"Langfuse 初始化失败（非致命）: {e}")

    # 初始化浏览器池
    try:
        from app.services.browser_pool import browser_pool
        from app.config import settings
        if settings.browser_pool_enabled:
            await browser_pool.initialize()
            logger.info("[ClipMind] Browser pool initialized")
    except Exception as e:
        logger.warning(f"[ClipMind] Browser pool initialization failed (will retry on demand): {e}")

    from app.routers.douyin_auth import start_session_gc, stop_session_gc
    start_session_gc()

    # 预热 QR 生成页已移除：改为用户点击扫码时才创建可见浏览器窗口
    # 避免应用启动时和后台刷新时弹出不必要的浏览器窗口

    # 启动定期数据同步检查后台任务（默认每6小时检查一次）
    try:
        from app.services.data_syncer import DataSyncer
        from app.services.rag import RAGService
        sync_rag = RAGService(collection_name="bilibili_videos")
        _syncer = DataSyncer(async_session_factory, sync_rag)
        asyncio.create_task(_syncer.schedule_sync_check(interval_hours=6))
        logger.info("[ClipMind] 定期数据同步检查后台任务已启动")
    except ValueError as _e:
        logger.error(f"[ClipMind] 定期同步检查启动失败：未配置 LLM API Key，请在设置中配置后再启用入库和同步功能: {_e}")
    except Exception as _e:
        logger.warning(f"[ClipMind] 定期同步检查启动失败（可忽略）: {_e}")

    # 启动时清理上次崩溃遗留的僵尸任务（RUNNING/PENDING），再自动重试失败任务
    try:
        from app.services.task_tracker_service import task_tracker
        cleaned = await task_tracker.cleanup_zombie_tasks()
        if cleaned:
            logger.info(f"[ClipMind] 僵尸任务清理: {cleaned} 个任务已标记为失败")
        retried = await task_tracker.auto_retry_failed_tasks()
        if retried:
            logger.info(f"[ClipMind] 启动自动重试: {len(retried)} 个任务已重启")
            await _reschedule_retry_tasks(retried)
    except Exception as _e:
        logger.warning(f"[ClipMind] 启动任务恢复失败（可忽略）: {_e}")

    # 恢复断点续传的入库任务（IngestTask）
    # 查询 status in (pending, running) 的任务，running 重置为 pending，
    # 用 _spawn_background_task 重新调度（从当前 stage 继续）。
    try:
        await _resume_ingest_tasks()
    except Exception as _e:
        logger.warning(f"[ClipMind] 入库任务恢复失败（可忽略）: {_e}")

    # 启动入库流水线单例（5 阶段并发，ASR 受 Semaphore 限流）
    # 单例惰性创建于 get_pipeline()，此处预启动 consumer，供批量入库并发使用。
    try:
        from app.services.ingest_pipeline import get_pipeline
        await get_pipeline()
        logger.info("[ClipMind] 入库流水线已启动")
    except Exception as _e:
        logger.warning(f"[ClipMind] 入库流水线启动失败（可忽略）: {_e}")

    # 清理上次进程崩溃遗留的模型下载任务状态
    try:
        from app.routers.model_market import cleanup_interrupted_tasks
        cleanup_interrupted_tasks()
    except Exception as _e:
        logger.warning(f"[ClipMind] 模型下载任务清理失败（可忽略）: {_e}")

    yield
    logger.info("[ClipMind] Shutting down")
    # 关闭 QR 预热池
    try:
        from app.routers.douyin_auth import stop_qr_pool
        await stop_qr_pool()
    except Exception as e:
        logger.debug(f"[ClipMind] QR pool stop error: {e}")
    await stop_session_gc()

    # 关闭浏览器池
    try:
        from app.services.browser_pool import browser_pool
        await browser_pool.close()
        logger.info("[ClipMind] Browser pool closed")
    except Exception as e:
        logger.debug(f"[ClipMind] Browser pool close error: {e}")

    # 关闭入库流水线单例
    try:
        from app.services.ingest_pipeline import shutdown_pipeline
        await shutdown_pipeline()
        logger.info("[ClipMind] 入库流水线已停止")
    except Exception as e:
        logger.debug(f"[ClipMind] 入库流水线停止失败: {e}")

    # 关闭 Langfuse，刷新所有待发送数据
    try:
        from app.services.langfuse_tracer import shutdown as shutdown_langfuse
        shutdown_langfuse()
    except Exception as e:
        logger.debug(f"[ClipMind] Langfuse shutdown error: {e}")


app = FastAPI(
    title="ClipMind 知识库系统",
    description="将你的 B站/抖音收藏夹变成可对话的知识库",
    version="0.1.0",
    lifespan=lifespan,
)


# CORS：桌面应用模式下允许 tauri:// 和 localhost
_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "X-App-Password"],
)


# ====== 访问密码中间件 ======
# 当 settings.auth_password 非空时，所有 API 请求需携带
# Authorization: Bearer <password> 或 X-App-Password: <password>。
# 放行路径：登录接口、API 文档、健康检查、OPTIONS 预检、前端静态资源。
class AppPasswordMiddleware(BaseHTTPMiddleware):
    """应用访问密码中间件：auth_password 非空时校验所有 API 请求。"""

    # 无需密码即可访问的路径前缀（精确匹配或前缀匹配）
    _PUBLIC_PREFIXES = (
        "/api/auth/login",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/health",
        "/_next",  # 前端 Next.js 静态资源
    )

    async def dispatch(self, request, call_next):
        # 动态读取 settings，确保热加载 / 测试 monkeypatch 立即生效
        import app.config as _cfg
        password = _cfg.settings.auth_password

        # 无密码保护 → 直接放行（不影响现有行为）
        if not password:
            return await call_next(request)

        # OPTIONS 预检请求放行（由 CORS 中间件处理）
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        # 公开路径放行
        if any(path == p or path.startswith(p + "/") or path.startswith(p) for p in self._PUBLIC_PREFIXES):
            return await call_next(request)

        # 校验 Authorization: Bearer <password> 或 X-App-Password: <password>
        auth_header = request.headers.get("Authorization", "")
        x_password = request.headers.get("X-App-Password", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = x_password.strip()

        if token and token == password:
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "未授权：缺少或错误的访问密码", "ok": False},
        )


# 注意：中间件添加顺序中，先 add 的为外层。CORS 先添加为最外层处理预检，
# AppPassword 后添加为内层，仅对实际请求校验密码。
app.add_middleware(AppPasswordMiddleware)


# 注册 API 路由
app.include_router(auth.router)
app.include_router(favorites.router)
app.include_router(knowledge.router)
app.include_router(chat.router)
app.include_router(douyin.router)
app.include_router(douyin_auth.router)
app.include_router(settings_router.router)
app.include_router(settings_router.ollama_router)
app.include_router(sync_router.router)
app.include_router(tasks_router.router)
app.include_router(system.router)
app.include_router(model_market.router)


@app.get("/api")
async def api_root():
    return {
        "message": "ClipMind API",
        "version": "0.1.0",
        "docs": "/docs",
        "status": "running",
    }


# ====== 应用访问密码登录接口 ======
class AppLoginRequest(BaseModel):
    """应用登录请求体。"""
    password: str = ""


@app.post("/api/auth/login")
async def app_login(payload: AppLoginRequest):
    """验证访问密码并返回 token。

    - auth_password 为空（未设置密码）→ 返回 ok=True，无需登录
    - 密码匹配 → 返回 ok=True + token（token = password 本身）
    - 密码不匹配 → 返回 401
    """
    import app.config as _cfg
    stored = _cfg.settings.auth_password

    # 未设置访问密码 → 无需登录
    if not stored:
        return {"ok": True, "token": "", "message": "未设置访问密码，无需登录"}

    if payload.password == stored:
        return {"ok": True, "token": payload.password}

    return JSONResponse(
        status_code=401,
        content={"detail": "密码错误", "ok": False},
    )


@app.get("/health")
async def health_check():
    try:
        async with get_db_context() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "ok"}
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": "database error"},
        )


# ====== 外部浏览器打开链接 ======
# Tauri webview 中 window.open / <a target="_blank"> 会在内部 webview 打开，
# 导致用户看不到窗口但能听到视频声音。通过后端调用系统默认浏览器解决。
class OpenExternalRequest(BaseModel):
    url: str


@app.post("/api/open-external")
async def open_external(payload: OpenExternalRequest):
    """在系统默认浏览器中打开指定 URL（仅允许 http/https）。"""
    import webbrowser
    url = payload.url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "仅支持 http/https 链接"})
    try:
        # webbrowser.open 是同步调用，用 to_thread 避免阻塞事件循环
        await asyncio.to_thread(webbrowser.open, url)
        return {"ok": True}
    except Exception as e:
        logger.error(f"打开外部浏览器失败: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# === 前端静态文件托管 ===
# 定位 Next.js 静态导出目录（frontend/out/）。
#
# PyInstaller onedir 打包后 `__file__` 不可靠（可能指向临时解压目录），
# 必须用 sys.executable 定位真实安装路径：
#   打包后 exe 在 resources/clipmind-backend/clipmind-backend.exe，
#   前端静态文件随 spec datas 一起放在同目录的 frontend/out/ 下
#   （由 release.yml 复制 + clipmind-backend.spec datas 收集）。
#
# 搜索顺序：
#   1. 打包模式：exe 同级目录 / frontend/out
#   2. 打包模式：exe 同级目录 / frontend_dist（兼容旧路径）
#   3. 开发模式：项目根 / frontend/out
#   4. 开发模式：项目根 / frontend_dist
if getattr(sys, "frozen", False):
    _APP_BASE = Path(sys.executable).resolve().parent
else:
    _APP_BASE = Path(__file__).resolve().parent.parent

_FRONTEND_DIR = _APP_BASE / "frontend" / "out"
if not _FRONTEND_DIR.exists():
    # PyInstaller 6.x onedir puts datas under _internal/ subdirectory
    _FRONTEND_DIR = _APP_BASE / "_internal" / "frontend" / "out"
if not _FRONTEND_DIR.exists():
    _FRONTEND_DIR = _APP_BASE / "frontend_dist"
if not _FRONTEND_DIR.exists() and not getattr(sys, "frozen", False):
    # 开发模式兜底：从 main.py 所在的 app/ 向上找项目根的 frontend/out
    _FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "out"

if _FRONTEND_DIR.exists():
    # 挂载 Next.js 静态资源目录（_next/）
    _static_dir = _FRONTEND_DIR / "_next"
    if _static_dir.exists():
        app.mount("/_next", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str, request: Request):
        """SPA fallback：非 API 路由返回 index.html。"""
        # 排除 API 路径
        if full_path.startswith(("api", "auth", "favorites", "knowledge", "chat", "douyin", "settings", "sync", "health", "docs", "tasks", "system")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        file_path = _FRONTEND_DIR / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))

        # SPA fallback
        index_path = _FRONTEND_DIR / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return JSONResponse({"detail": "Frontend not built"}, status_code=404)
else:
    @app.get("/")
    async def root():
        return {
            "message": "ClipMind 知识库系统",
            "version": "0.1.0",
            "docs": "/docs",
            "status": "running",
            "hint": "前端静态文件未找到，请先构建前端",
        }


if __name__ == "__main__":
    import uvicorn
    # PyInstaller frozen 环境下必须直接传 app 实例，不能用字符串导入路径：
    # 1. 字符串导入会让 uvicorn 用 importlib.import_module("app.main") 再执行一次模块级代码
    #    （FastAPI 实例化、日志句柄、数据库引擎全部被重复创建）
    # 2. reload=True 会 spawn 子进程，frozen 环境下缺少 multiprocessing.freeze_support()
    #    会导致子进程无限递归或立即退出 → 后端进程启动后秒退
    # 因此这里强制 reload=False，并直接传 app 实例。
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level="info",
    )