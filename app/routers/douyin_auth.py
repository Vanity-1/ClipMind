"""
ClipMind -- Douyin Auth router (v6)
Playwright-based QR code login with element screenshot extraction.
Captures QR code directly from the DOM element, not the full page.
"""
import asyncio
import base64
import struct
import time
import uuid
from typing import Optional
import httpx
from fastapi import APIRouter, HTTPException, Query, Depends, BackgroundTasks
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import UserSession as UserSessionModel, Platform
from app.services.crypto import encrypt_secret, decrypt_secret
from app.services.tracing import TraceContext, trace_logger, set_trace_id

try:
    # playwright 是运行时依赖（_get_browser 等函数使用），但为避免在测试/纯路由加载环境
    # 下因未安装而失败，这里容错导入。生产环境需安装 playwright 才能真正使用抖音登录。
    from playwright.async_api import PlaywrightError
except ImportError:
    PlaywrightError = type("PlaywrightError", (Exception,), {})  # type: ignore[assignment]

router = APIRouter(prefix="/douyin/auth", tags=["抖音认证"])
_login_sessions: dict[str, dict] = {}
_browser = None
_playwright = None
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STEALTH = """Object.defineProperty(navigator, "webdriver", { get: () => false }); window.chrome = { runtime: {} }; Object.defineProperty(navigator, "plugins", { get: () => [1,2,3,4,5] }); Object.defineProperty(navigator, "languages", { get: () => ["zh-CN","zh","en"] }); var oq = navigator.permissions.query; navigator.permissions.query = (p) => p.name === "notifications" ? Promise.resolve({state: Notification.permission}) : oq(p);"""

async def _qr_route_handler(route):
    """拦截重型资源，仅放行 QR 捕获所需请求。

    abort: image/media/font/stylesheet；放行 document/script/xhr/fetch/websocket/other。
    网络拦截主路径（get_qrcode XHR）与 SSO API（page.evaluate fetch）属 xhr/fetch，不受影响。
    """
    if route.request.resource_type in ("image", "media", "font", "stylesheet"):
        await route.abort()
    else:
        await route.continue_()

class QRCodeResponse(BaseModel):
    session_key: str
    qrcode_image_base64: str
    message: str = "请用抖音 App 扫描二维码"


class QRCodePollResponse(BaseModel):
    status: str
    message: str
    session_id: Optional[str] = None
    user_info: Optional[dict] = None


class AuthStatusResponse(BaseModel):
    logged_in: bool
    uid: Optional[str] = None
    nickname: Optional[str] = None


class LogoutResponse(BaseModel):
    message: str


class CookieLoginRequest(BaseModel):
    cookie: str


class CookieLoginResponse(BaseModel):
    success: bool
    message: str
    uid: str = ""
    nickname: str = ""


class DouyinSyncFolderStat(BaseModel):
    """抖音同步单项统计（喜欢/收藏）"""
    synced: int
    new: int
    folder_id: Optional[int] = None


class DouyinSyncResult(BaseModel):
    """抖音同步结果统一响应结构

    用于统一 sync_favorites 的所有 return 分支，避免字段不一致导致前端类型歧义。
    - collect 字段恒为 None：listcollection 端点已失效，仅保留兼容字段
    - collect_flat：以文件夹汇总方式返回收藏视频统计
    """
    success: bool
    first_sync: Optional[bool] = None
    like: Optional[DouyinSyncFolderStat] = None
    collect: Optional[dict] = None
    collect_flat: Optional[DouyinSyncFolderStat] = None
    message: Optional[str] = None


_browser_lock = asyncio.Lock()

# 持有 create_task 创建的后台任务引用，避免被 GC 中断
# 任务完成后通过 done callback 自动从 set 移除
_pending_tasks: set = set()


def _spawn_background_task(coro):
    """创建后台任务并持有引用，避免 GC 中断。

    Python 官方文档明确警告：asyncio.create_task 返回的任务若未被引用，
    可能被垃圾回收中断。本函数统一管理任务引用。
    """
    task = asyncio.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task

# 二维码缓存：有效期内复用，避免反复生成
_qr_cache: dict = {}
_QR_CACHE_TTL_SEC = 50  # 抖音QR码有效期约60秒，缓存TTL设为50秒确保不会返回过期QR码


def _get_cached_qr() -> Optional[dict]:
    """返回未过期的缓存 QR dict {b64, token, session_key} 或 None。

    命中条件：缓存存在 且 age < _QR_CACHE_TTL_SEC 且 对应 session_key 仍处于 waiting 状态
    且 QR码图像通过有效性验证。
    """
    if not _qr_cache:
        return None
    created = _qr_cache.get("created_at")
    if created is None or (time.monotonic() - created) >= _QR_CACHE_TTL_SEC:
        return None
    # 额外验证：确保缓存的QR码图像仍然有效
    cached_b64 = _qr_cache.get("b64")
    if not cached_b64 or not _validate_qr_image(cached_b64):
        _qr_cache.clear()
        return None
    sk = _qr_cache.get("session_key")
    if sk:
        st = _login_sessions.get(sk)
        if st and st.get("status") not in ("waiting", "scanned"):
            # 会话已结束，缓存失效
            _qr_cache.clear()
            return None
    return dict(_qr_cache)

def _set_qr_cache(b64: str, token: Optional[str], session_key: str) -> None:
    _qr_cache.clear()
    _qr_cache.update({
        "b64": b64, "token": token,
        "session_key": session_key,
        "created_at": time.monotonic(),
    })

def _invalidate_qr_cache() -> None:
    _qr_cache.clear()


def _validate_qr_image(b64_data: str) -> bool:
    """验证QR码base64数据是否为有效的图片。

    不仅验证base64格式合法性，还验证：
    1. 图片格式（PNG/JPG/GIF）
    2. 图片尺寸（宽高均 >= 100px，且接近正方形）
    3. 文件大小（>= 300字节，简单QR码PNG可能很小）

    返回 True 表示验证通过，False 表示无效。
    防止将空白透明图、小图标等非QR码数据当作二维码返回。
    """
    if not b64_data or len(b64_data) < 100:
        return False
    try:
        data = base64.b64decode(b64_data, validate=True)
    except Exception:
        return False

    size = len(data)
    if size < 300:  # 最小阈值，避免太小的图标
        return False

    # 检测图片格式
    is_png = data[:8] == b'\x89PNG\r\n\x1a\n'
    is_jpg = data[:2] == b'\xff\xd8'
    is_gif = data[:6] in (b'GIF87a', b'GIF89a')

    if not (is_png or is_jpg or is_gif):
        return False

    # PNG: 从 IHDR chunk 读取宽高
    if is_png and len(data) > 24:
        try:
            width = struct.unpack('>I', data[16:20])[0]
            height = struct.unpack('>I', data[20:24])[0]
            if width < 100 or height < 100:
                return False
            # 额外检查：宽高应该大致相等（QR码是正方形）
            if abs(width - height) > max(width, height) * 0.15:
                return False
            return True
        except Exception:
            return False

    # JPG / GIF 无法快速获取尺寸，靠大小阈值判断
    return size >= 2000


# QR 生成页预热池：启动时预创建已导航到 douyin.com 的页面，消除冷启动与导航开销
_qr_page_pool: list = []  # 元素: {"ctx","page","captured","on_response","qr_event","warmed_at"}
_qr_pool_lock = asyncio.Lock()
_QR_POOL_TARGET_SIZE = 1  # 1 个预热页即可，窗口在屏幕外不需要多个
_QR_POOL_MAX_AGE_SEC = 300  # 池中预热页超过此年龄则丢弃重建（页面可复用，QR码通过后台刷新维持新鲜）
_QR_POOL_HEALTH_CHECK_INTERVAL = 60  # 健康检查间隔（秒）：每分钟检查一次池中页面是否存活

_qr_pool_health_task = None  # 健康检查后台任务引用
_qr_pool_health_stop = asyncio.Event()  # 停止信号

async def _kill_zombie_chrome() -> None:
    """清理僵尸 chromium 进程。

    只清理本模块管理的 _browser 对应的已知 chromium 进程，
    不使用 pgrep 全局杀伤，避免误杀 sync 任务的独立浏览器实例。

    策略：
    1. 优先通过 playwright 正常关闭 browser.close()。
    2. 仅当正常关闭失败时，才尝试获取 _browser 对应的已知 PID 并用
       os.kill 杀掉，绝不使用 pgrep 全局扫描。
    3. playwright 不同版本获取 PID 的属性路径不同，全部做 try/except 保护。
    """
    import os
    global _browser, _playwright
    if _browser is not None:
        try:
            # 优先通过 playwright 正常关闭
            await _browser.close()
        except Exception as e:
            logger.debug(f"[Douyin] 正常关闭 browser 失败，尝试 OS 级清理: {e}")
            # 兜底：尝试获取 browser 对应的已知 PID 并杀掉（只杀已知 PID，不全局扫描）
            try:
                pid = None
                # playwright 不同版本属性路径可能不同，做保护
                for attr_path in (
                    lambda: _browser._impl._process._pid,
                    lambda: _browser._impl_obj._transport._proc.pid,
                    lambda: _browser._connection._transport._proc.pid,
                ):
                    try:
                        pid = attr_path()
                        if pid:
                            break
                    except Exception:
                        continue
                if pid:
                    try:
                        os.kill(pid, 9)
                        logger.info(f"[Douyin] 已杀掉僵尸 chromium 进程: PID={pid}")
                    except ProcessLookupError:
                        pass  # 进程已退出
                    except Exception as kill_err:
                        logger.warning(f"[Douyin] 杀掉僵尸进程失败 PID={pid}: {kill_err}")
            except Exception as cleanup_err:
                logger.warning(f"[Douyin] OS 级清理失败: {cleanup_err}")
        finally:
            _browser = None

    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception as e:
            logger.debug(f"[Douyin] playwright.stop 异常: {e}")
        finally:
            _playwright = None

async def _force_dispose():
    """Thoroughly dispose of browser and playwright."""
    global _browser, _playwright
    if _browser is not None:
        try:
            await _browser.close()
        except Exception as e:
            logger.debug(f"[Douyin] browser.close error: {e}")
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception as e:
            logger.debug(f"[Douyin] playwright.stop error: {e}")
        _playwright = None
    await _kill_zombie_chrome()

class BrowserLaunchError(RuntimeError):
    """浏览器启动失败的基类，携带分类信息便于上层映射到不同的 HTTP 响应。"""
    def __init__(self, message: str, error_type: str = "unknown", detail: str = ""):
        super().__init__(message)
        self.error_type = error_type
        self.detail = detail


class BrowserDependencyMissingError(BrowserLaunchError):
    """Playwright 或 chromium 未安装。"""
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message, error_type="dependency_missing", detail=detail)


class BrowserTimeoutError(BrowserLaunchError):
    """浏览器启动超时。"""
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message, error_type="timeout", detail=detail)


class BrowserPlatformError(BrowserLaunchError):
    """平台兼容性问题（如 Windows 事件循环）。"""
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message, error_type="platform", detail=detail)


async def _diagnose_browser_env() -> dict:
    """预检浏览器运行环境，返回诊断结果。

    用于启动失败时提供更精确的错误信息，也可用于健康检查接口。
    """
    result = {
        "playwright_installed": False,
        "chromium_available": False,
        "ffmpeg_available": False,
        "platform": __import__("sys").platform,
        "details": [],
    }
    import sys
    # 1. 检查 playwright Python 包
    try:
        import playwright  # noqa: F401
        result["playwright_installed"] = True
    except ImportError:
        result["details"].append("playwright Python 包未安装")

    # 2. 检查 chromium 浏览器是否可用
    if result["playwright_installed"]:
        try:
            from playwright.sync_api import sync_playwright
            # 仅尝试获取可执行文件路径，不启动浏览器
            with sync_playwright() as p:
                exec_path = p.chromium.executable_path
                if exec_path:
                    result["chromium_available"] = True
                    result["details"].append(f"chromium 路径: {exec_path}")
        except Exception as e:
            result["details"].append(f"chromium 检查失败: {type(e).__name__}: {e}")

    # 3. 检查 ffmpeg（ASR 功能需要）
    try:
        import shutil
        if shutil.which("ffmpeg"):
            result["ffmpeg_available"] = True
    except Exception:
        pass

    return result


async def _get_browser():
    """Get or create a browser instance. Thread-safe with lock + retry.

    抛出 BrowserLaunchError 的子类，便于上层根据 error_type 返回不同的错误信息。
    """
    global _browser, _playwright

    # Windows 事件循环修复：uvicorn --reload 等场景下 worker 进程可能
    # 使用 SelectorEventLoop，不支持子进程（Playwright 启动浏览器会失败）。
    # 这里检测并动态切换为 ProactorEventLoop。
    import sys
    if sys.platform == "win32":
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            from asyncio import SelectorEventLoop, ProactorEventLoop
            if isinstance(loop, SelectorEventLoop):
                logger.warning(
                    "[Douyin] Detected SelectorEventLoop on Windows, "
                    "switching to ProactorEventLoop for Playwright compatibility"
                )
                # 不能直接替换正在运行的 loop，但我们可以设置 policy，
                # 让后续创建的子事件循环使用 Proactor。
                # 更稳妥的方式：用 asyncio.to_thread 在新线程中启动浏览器，
                # 新线程会使用新的事件循环（受 policy 影响）。
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except RuntimeError:
            # 无运行中的 loop，直接设置 policy 即可
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async with _browser_lock:
        # Health check: use .version (fast, doesn't need contexts)
        if _browser is not None:
            try:
                _browser.version
                return _browser
            except Exception as e:
                logger.warning(f"[Douyin] Browser health check failed: {e}, recreating...")
                await _force_dispose()

        # Ensure clean slate
        await _force_dispose()

        # Import playwright
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise BrowserDependencyMissingError(
                "Playwright 未安装，请执行 pip install playwright && playwright install chromium",
                detail=f"ImportError: {e}"
            ) from e

        # Retry loop
        last_err = None
        for attempt in range(3):
            try:
                _playwright = await asyncio.wait_for(
                    async_playwright().start(), timeout=10
                )
                _browser = await asyncio.wait_for(
                    _playwright.chromium.launch(
                        headless=False,  # 可见模式，用户扫码和二次验证都在此窗口完成
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-blink-features=AutomationControlled",
                            "--disable-dev-shm-usage",
                            "--window-size=480,720",
                        ]
                    ), timeout=20
                )
                logger.info(f"[Douyin] Browser started (attempt {attempt+1})")
                return _browser
            except asyncio.TimeoutError as e:
                last_err = e
                logger.error(
                    f"[Douyin] Launch attempt {attempt+1}/3 timed out: {e}"
                )
                await _force_dispose()
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except NotImplementedError as e:
                # Windows 上 SelectorEventLoop 不支持子进程会抛 NotImplementedError
                last_err = e
                logger.error(
                    f"[Douyin] Launch attempt {attempt+1}/3 failed (platform error): "
                    f"{type(e).__name__}: {e}"
                )
                await _force_dispose()
                raise BrowserPlatformError(
                    "浏览器启动失败：Windows 事件循环配置异常，请确保使用 ProactorEventLoop",
                    detail=f"NotImplementedError: {e}"
                ) from e
            except Exception as e:
                last_err = e
                logger.error(
                    f"[Douyin] Launch attempt {attempt+1}/3 failed: "
                    f"{type(e).__name__}: {e}"
                )
                await _force_dispose()
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        # 3次重试都失败了，根据最后一次错误类型分类
        import sys
        diag = await _diagnose_browser_env()
        diag_str = "; ".join(diag["details"]) if diag["details"] else ""

        if isinstance(last_err, asyncio.TimeoutError):
            raise BrowserTimeoutError(
                "浏览器启动超时，请检查系统资源或稍后重试",
                detail=f"Timeout after 3 attempts. Diagnostics: {diag_str}"
            ) from last_err

        if not diag["chromium_available"]:
            raise BrowserDependencyMissingError(
                "Chromium 浏览器未安装，请执行 playwright install chromium",
                detail=f"Diagnostics: {diag_str}"
            )

        raise BrowserLaunchError(
            f"浏览器启动失败（{type(last_err).__name__}），请稍后重试",
            error_type="unknown",
            detail=f"{type(last_err).__name__}: {last_err}. Diagnostics: {diag_str}"
        ) from last_err

async def _create_page(browser):
    ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width":1920,"height":1080}, locale="zh-CN")
    await ctx.add_init_script(STEALTH)
    page = await ctx.new_page()
    # 注意：不再使用 _qr_route_handler 拦截资源。
    # 之前拦截 image/media/font/stylesheet 会导致抖音页面无法正常加载（domcontentloaded 都触发不了），
    # 反而会导致登录弹窗和 QR 码无法加载。
    # 让页面完整加载虽然慢几秒，但能确保功能正常。
    return ctx, page


class QRCaptureError(RuntimeError):
    """二维码获取失败，携带阶段和诊断信息。"""
    def __init__(self, message: str, stage: str = "unknown", elapsed_sec: float = 0,
                 detail: str = "", screenshot_b64: str = ""):
        super().__init__(message)
        self.stage = stage
        self.elapsed_sec = elapsed_sec
        self.detail = detail
        self.screenshot_b64 = screenshot_b64


async def _safe_page_screenshot(page, max_size_bytes: int = 200 * 1024) -> str:
    """安全地截取页面截图，失败时返回空字符串。

    仅用于诊断，不会影响主流程。返回 base64（不含 data:image 前缀）。
    """
    try:
        if page.is_closed():
            return ""
        shot = await asyncio.wait_for(page.screenshot(full_page=True, type="jpeg", quality=60), timeout=8)
        if isinstance(shot, bytes) and 0 < len(shot) <= max_size_bytes:
            return base64.b64encode(shot).decode("ascii")
    except Exception:
        pass
    return ""


async def _collect_all_cookies(ctx) -> list:
    """从浏览器上下文中收集所有相关 domain 的 cookie，去重后返回。

    抖音登录 cookie 可能分散在多个 domain 下，需要统一收集才能确保
    后续数据抓取时 cookie 完整有效。
    """
    # 抖音相关的所有可能 domain（包含子域通配）
    douyin_domains = [
        ".douyin.com",
        "www.douyin.com",
        "login.douyin.com",
        "sso.douyin.com",
        ".toutiao.com",
        "www.toutiao.com",
    ]
    seen = set()
    all_cookies = []
    for domain in douyin_domains:
        try:
            cks = await asyncio.wait_for(ctx.cookies(domain=domain), timeout=5)
            for c in cks:
                key = f"{c['name']}@{c.get('domain','')}@{c.get('path','/')}"
                if key not in seen:
                    seen.add(key)
                    all_cookies.append(c)
        except Exception:
            continue
    # 兜底：直接获取所有 cookie（不分 domain）
    if not all_cookies:
        try:
            all_cookies = await asyncio.wait_for(ctx.cookies(), timeout=5)
        except Exception:
            all_cookies = []
    return all_cookies


def _cookies_to_str(cookies: list) -> str:
    """将 cookie 列表转为 name=value; 格式字符串。"""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


async def _trigger_login_click(page, max_attempts: int = 2) -> bool:
    """点击"登录"按钮触发登录弹窗，使页面自身发起 get_qrcode 请求。

    抖音首页不会自动加载二维码，必须点击"登录"按钮打开弹窗才会触发
    get_qrcode API（页面自身请求带完整签名，不会被反爬拦截）。

    两级降级策略（从快到慢，每级都有超时保护）：
      Level 1: locator.click() — Playwright 原生 CDP 鼠标事件，不注入大段 JS
      Level 2: locator.evaluate("el => el.click()") — 注入一行 JS，较轻量

    注意：不使用 page.evaluate 全量 DOM 扫描。抖音页面 JS 极重，
    page.evaluate 会阻塞整个 CDP 通道 30-60 秒，且 asyncio.wait_for
    无法真正取消已发出的 CDP 请求，会导致后续所有操作都被阻塞。
    Level 1+2 已覆盖足够多的选择器，可靠性足够。

    返回 True 表示成功点击，False 表示失败。
    """
    # 选择器列表：从最精确到最宽泛，逐级尝试
    selectors = [
        'button:has-text("登录")',
        'a:has-text("登录")',
        '[data-e2e*="login" i]',
        '[class*="login" i] button',
        '[class*="login" i] a',
        'div[role="button"]:has-text("登录")',
    ]

    for attempt in range(max_attempts):
        logger.info(f"[Douyin] Login button click attempt {attempt + 1}/{max_attempts}")
        click_start = time.monotonic()

        # Level 1: locator.click() — 原生 CDP 鼠标事件（最快，不注入大段 JS）
        level1_ok = False
        for sel in selectors:
            try:
                locator = page.locator(sel).first
                await locator.click(timeout=5000)
                elapsed = time.monotonic() - click_start
                logger.info(f"[Douyin] Clicked login via locator.click (selector={sel}, {elapsed:.1f}s)")
                await asyncio.sleep(2)
                return True
            except Exception as e:
                logger.debug(f"[Douyin] locator.click failed for {sel}: {type(e).__name__}")
                continue

        # Level 2: locator.evaluate("el => el.click()") — 注入一行 JS，较轻量
        for sel in selectors:
            try:
                locator = page.locator(sel).first
                await locator.wait_for(state="attached", timeout=3000)
                await locator.evaluate("el => el.click()")
                elapsed = time.monotonic() - click_start
                logger.info(f"[Douyin] Clicked login via locator.evaluate (selector={sel}, {elapsed:.1f}s)")
                await asyncio.sleep(2)
                return True
            except Exception as e:
                logger.debug(f"[Douyin] locator.evaluate failed for {sel}: {type(e).__name__}")
                continue

        if attempt < max_attempts - 1:
            logger.debug(f"[Douyin] No login button found, waiting 2s for SPA to render...")
            await asyncio.sleep(2)

    logger.warning("[Douyin] No login button found after all attempts")
    return False

async def _warmup_one_qr_page() -> bool:
    """创建一个预热 QR 页（含路由拦截+STEALTH+QR 网络监听+Event），导航到带 modal_id=login 的抖音 URL，自动弹出登录弹窗并加载 QR 码。

    返回 True 表示成功入池，False 表示失败（浏览器未就绪等）。
    关键优化：直接访问 https://www.douyin.com/?modal_id=login，页面会自动弹出登录弹窗并加载 QR 码，
    完全不需要点击登录按钮，绕开了 CDP 被页面重 JS 阻塞的问题。
    耗时从 120-150s 降到 10-15s。
    """
    try:
        b = await _get_browser()
    except Exception as e:
        logger.warning(f"[Douyin] warmup: browser unavailable: {e}")
        return False
    try:
        ctx, page = await _create_page(b)
        captured, on_response, qr_event, qrconnect_status = await _capture_qr_via_network(page)
        try:
            # 导航到带登录弹窗参数的 URL，页面会自动弹出登录弹窗并加载 QR 码
            # 不需要点击登录按钮，绕开 CDP 阻塞问题
            await page.goto("https://www.douyin.com/?modal_id=login", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.debug(f"[Douyin] warmup goto modal login tolerated: {e}")
        # 等待网络拦截捕获 QR 码（最多 30s，通常 10-15s）
        try:
            await asyncio.wait_for(qr_event.wait(), timeout=30)
            logger.info(f"[Douyin] warmup: QR captured during warmup (b64 len={len(captured.get('b64','') or '')})")
        except asyncio.TimeoutError:
            logger.info("[Douyin] warmup: QR not captured in 30s, will be fetched on-demand")
        entry = {
            "ctx": ctx, "page": page,
            "captured": captured, "on_response": on_response, "qr_event": qr_event,
            "qrconnect_status": qrconnect_status,
            "warmed_at": time.monotonic(),
        }
        _qr_page_pool.append(entry)
        logger.info(f"[Douyin] warmup: QR page ready (pool size={len(_qr_page_pool)})")
        return True
    except Exception as e:
        logger.warning(f"[Douyin] warmup create page failed: {e}")
        return False

async def acquire_qr_page():
    """从预热池借一个 QR 页；池空或过期则即时创建。借用后后台补充池。

    返回 dict: {ctx, page, captured, on_response, qr_event, from_warmup}。
    从池取出时 captured/qr_event 可能已就绪（预热期已捕获 QR）。
    """
    async with _qr_pool_lock:
        # 清理过期预热页
        now = time.monotonic()
        entry = None  # 必须在循环前初始化，避免 break 退出时 entry 未定义
        while _qr_page_pool:
            top = _qr_page_pool[0]
            if now - top["warmed_at"] > _QR_POOL_MAX_AGE_SEC:
                # 已捕获到 QR 的预热页不过期，直接返回使用（避免浪费已捕获的二维码）
                if top["captured"].get("b64"):
                    entry = _qr_page_pool.pop(0)
                    entry["from_warmup"] = True
                    logger.info("[Douyin] warmup: reusing stale page with captured QR")
                    break
                _qr_page_pool.pop(0)
                logger.info("[Douyin] warmup: discarded stale prewarmed page")
                try:
                    await top["ctx"].close()
                except Exception:
                    pass
                continue
            # 顶部页面未过期，取出使用
            entry = _qr_page_pool.pop(0)
            entry["from_warmup"] = True
            break
        if entry is None and _qr_page_pool:
            entry = _qr_page_pool.pop(0)
            entry["from_warmup"] = True

    if entry is not None:
        # 不再后台补充预热池，避免弹出不必要的浏览器窗口
        return entry

    # 池空：即时创建（导航到带 modal_id=login 的 URL，自动弹出登录弹窗）
    logger.info("[Douyin] QR pool empty, creating page on-demand")
    b = await _get_browser()
    ctx, page = await _create_page(b)
    captured, on_response, qr_event, qrconnect_status = await _capture_qr_via_network(page)
    try:
        # 导航到带登录弹窗参数的 URL，页面会自动弹出登录弹窗并加载 QR 码
        await page.goto("https://www.douyin.com/?modal_id=login", wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        logger.debug(f"[Douyin] on-demand goto modal login tolerated: {e}")
    # 不再后台补充预热池，避免弹出不必要的浏览器窗口
    return {
        "ctx": ctx, "page": page,
        "captured": captured, "on_response": on_response, "qr_event": qr_event,
        "qrconnect_status": qrconnect_status,
        "from_warmup": False,
    }

async def _refill_qr_pool() -> None:
    """后台补充预热池到目标大小（循环创建直到达标）。"""
    while True:
        async with _qr_pool_lock:
            need = _QR_POOL_TARGET_SIZE - len(_qr_page_pool)
        if need <= 0:
            return
        try:
            ok = await _warmup_one_qr_page()
            if not ok:
                return  # 浏览器不可用，停止补充
        except Exception as e:
            logger.debug(f"[Douyin] refill failed: {e}")
            return

async def start_qr_pool_warmup() -> None:
    """应用启动时预热：启动 browser 并预创建 QR 页。失败不阻塞应用启动。"""
    try:
        # 预热 browser（消除首次请求的浏览器冷启动）
        try:
            await _get_browser()
            logger.info("[Douyin] warmup: browser pre-launched")
        except Exception as e:
            logger.warning(f"[Douyin] warmup: browser pre-launch failed (will lazy start): {e}")
        # 预创建 QR 页（补充到目标大小）
        await _refill_qr_pool()
        # 启动 QR 刷新循环（后台持续保持 QR 码新鲜）
        start_warmup_qr_refresh()
        # 启动预热池健康检查（定期检测并自动恢复死亡页面）
        start_warmup_health_check()
    except Exception as e:
        logger.warning(f"[Douyin] warmup startup failed (non-fatal): {e}")

async def _check_page_alive(entry: dict) -> bool:
    """检查预热池中的页面是否仍然存活（未崩溃、未断开连接）。
    
    通过执行一个简单的 JS 来验证页面响应性。
    """
    try:
        page = entry["page"]
        # 简单的心跳检查：执行一个无副作用的 JS 调用
        result = await asyncio.wait_for(page.evaluate("1 + 1"), timeout=5)
        return result == 2
    except Exception as e:
        logger.debug(f"[Douyin] warmup health check: page dead: {e}")
        return False


async def _health_check_pool() -> None:
    """预热池健康检查：
    1. 检查池中每个页面是否存活
    2. 移除死亡页面并关闭资源
    3. 如果池大小不足，触发补充
    """
    try:
        async with _qr_pool_lock:
            pool_snapshot = list(_qr_page_pool)
        
        dead_entries = []
        alive_entries = []
        
        for entry in pool_snapshot:
            if await _check_page_alive(entry):
                alive_entries.append(entry)
            else:
                dead_entries.append(entry)
        
        if dead_entries:
            logger.warning(
                f"[Douyin] warmup health: found {len(dead_entries)} dead pages, "
                f"{len(alive_entries)} alive"
            )
            # 从池中移除死亡页面
            async with _qr_pool_lock:
                _qr_page_pool[:] = [
                    e for e in _qr_page_pool if e not in dead_entries
                ]
            # 关闭死亡页面的资源
            for entry in dead_entries:
                try:
                    await entry["ctx"].close()
                except Exception:
                    pass
            
            # 触发补充
            _spawn_background_task(_refill_qr_pool())
        else:
            logger.debug(f"[Douyin] warmup health: all {len(alive_entries)} pages alive")
    except Exception as e:
        logger.warning(f"[Douyin] warmup health check error: {e}")


async def _health_check_loop() -> None:
    """后台健康检查循环，按间隔定期执行。"""
    logger.info(
        f"[Douyin] warmup health check started (interval={_QR_POOL_HEALTH_CHECK_INTERVAL}s)"
    )
    while not _qr_pool_health_stop.is_set():
        try:
            await _health_check_pool()
        except Exception as e:
            logger.debug(f"[Douyin] warmup health loop error: {e}")
        # 等待间隔时间或停止信号
        try:
            await asyncio.wait_for(
                _qr_pool_health_stop.wait(),
                timeout=_QR_POOL_HEALTH_CHECK_INTERVAL,
            )
        except asyncio.TimeoutError:
            pass  # 正常超时，继续下一轮
    logger.info("[Douyin] warmup health check stopped")


def start_warmup_health_check() -> None:
    """启动预热池健康检查（如果尚未启动）。"""
    global _qr_pool_health_task
    if _qr_pool_health_task is not None and not _qr_pool_health_task.done():
        return  # 已在运行
    _qr_pool_health_stop.clear()
    _qr_pool_health_task = _spawn_background_task(_health_check_loop())


async def stop_warmup_health_check() -> None:
    """停止预热池健康检查。"""
    global _qr_pool_health_task
    _qr_pool_health_stop.set()
    if _qr_pool_health_task is not None:
        try:
            await asyncio.wait_for(_qr_pool_health_task, timeout=5)
        except Exception:
            pass
        _qr_pool_health_task = None


async def stop_qr_pool() -> None:
    """关闭预热池中所有 ctx/page，并清理浏览器资源。

    清理顺序：
    1. 停止 QR 刷新循环（避免刷新任务访问已关闭的页面）
    2. 停止健康检查（避免在清理过程中触发补充）
    3. 关闭预热池中的所有页面上下文
    4. 关闭浏览器实例和 Playwright（释放进程资源）
    """
    # 1. 停止 QR 刷新循环
    await stop_warmup_qr_refresh()
    # 2. 停止健康检查
    await stop_warmup_health_check()
    # 3. 清空刷新标记
    _refreshing_pages.clear()
    # 4. 关闭预热池页面
    async with _qr_pool_lock:
        pool = list(_qr_page_pool)
        _qr_page_pool.clear()
    for entry in pool:
        try:
            await entry["ctx"].close()
        except Exception:
            pass
    if pool:
        logger.info(f"[Douyin] warmup pool stopped, closed {len(pool)} pages")
    # 5. 关闭浏览器实例，释放进程资源
    await _force_dispose()
    logger.info("[Douyin] Browser disposed after QR pool stop")

# ---- QR Capture Selectors (v6: element screenshot) ----
_QR_SELECTORS = [
    "canvas[width>\"149\"][height>\"149\"]",
    ".qrcode-img img", ".qrcode-img canvas",
    ".qr-code img", ".qr-code canvas",
    ".scan-code img", ".scan-code canvas",
    "img[src*=\"qrcode\"]", "img[src*=\"qr_code\"]",
    "img[src*=\"douyin.com/qrcode\"]",
    "[class*=\"qr\"] img", "[class*=\"QR\"] img",
    "[class*=\"qrcode\"] img", "[class*=\"qrcode\"] canvas",
    ".login-modal img", ".login-container img",
    "#loginContainer img",
    "img[width>\"179\"]", "img[height>\"179\"]",
    "canvas[width>\"179\"]", "canvas[height>\"179\"]",
]

async def _dump_login_dom(page):
    """Dump DOM around login containers to discover QR selectors."""
    try:
        info = await page.evaluate("""() => {
            var result = [];
            // Find login-related containers
            var containers = document.querySelectorAll(
                '[class*="login" i], [class*="Login"], [class*="qrcode" i], [class*="QR" i], '
                + '[class*="mask" i], [class*="modal" i], [class*="overlay" i], [class*="dialog" i]'
            );
            for (var i = 0; i < containers.length; i++) {
                var el = containers[i];
                var tag = el.tagName;
                var cls = el.className || "";
                var id = el.id || "";
                var rect = el.getBoundingClientRect();
                var w = rect.width, h = rect.height;
                // Only report visible, reasonably sized containers
                if (w > 100 && h > 100) {
                    // Find child images/canvases
                    var children = [];
                    var imgs = el.querySelectorAll("img, canvas");
                    for (var j = 0; j < imgs.length; j++) {
                        var c = imgs[j];
                        var cr = c.getBoundingClientRect();
                        children.push(c.tagName + "(" + Math.round(cr.width) + "x" + Math.round(cr.height) + ")");
                    }
                    result.push({
                        tag: tag, cls: cls.substring(0, 60), id: id,
                        size: Math.round(w) + "x" + Math.round(h),
                        children: children.join(", ")
                    });
                }
            }
            return result;
        }""")
        if info:
            logger.info(f"[Douyin] Login DOM containers found: {len(info)}")
            for item in info[:10]:
                logger.info(f"  {item['tag']}#{item['id']} .{item['cls'][:40]} [{item['size']}] kids: {item['children']}")
        return info
    except Exception as e:
        logger.debug(f"[Douyin] DOM dump error: {e}")
        return []

async def _capture_qr_element(page):
    """Capture QR: first try login modal clip, then crop fallback."""
    
    # Step 1: Check if Douyin login modal is present
    modal_info = None
    try:
        modal_info = await page.evaluate("""() => {
            var modal = document.getElementById("douyin-login-new-id");
            if (!modal) {
                var modals = document.querySelectorAll('[class*="douyin_login" i]');
                if (modals.length > 0) modal = modals[0];
            }
            if (!modal) return null;
            var imgs = modal.querySelectorAll("img, canvas");
            for (var i = 0; i < imgs.length; i++) {
                var r = imgs[i].getBoundingClientRect();
                var w = r.width, h = r.height;
                var sq = Math.min(w,h) / Math.max(w,h);
                // QR is typically 150-250px and square-ish
                if (w >= 140 && h >= 140 && sq >= 0.85) {
                    return { x: r.x, y: r.y, w: w, h: h, tag: imgs[i].tagName };
                }
            }
            return null;
        }""")
    except Exception:
        pass

    if modal_info:
        clip = {"x": modal_info["x"], "y": modal_info["y"],
                "width": modal_info["w"], "height": modal_info["h"]}
        screenshot = await page.screenshot(type="png", clip=clip)
        b64 = base64.b64encode(screenshot).decode()
        logger.info(
            f"[Douyin] QR captured from login modal: "
            f"{modal_info['w']:.0f}x{modal_info['h']:.0f}px"
        )
        return b64

    # Step 2: No modal found — use crop fallback
    logger.info("[Douyin] No login modal found, will use crop fallback")
    return None
async def _capture_qr_fallback(page):
    """Fallback: use JS to find a large visible element and screenshot it."""
    try:
        result = await page.evaluate("""() => {
            var imgs = document.querySelectorAll("img");
            for (var i = 0; i < imgs.length; i++) {
                var img = imgs[i];
                var w = img.naturalWidth || img.offsetWidth || 0;
                var h = img.naturalHeight || img.offsetHeight || 0;
                if (w >= 160 && h >= 160) {
                    return { index: i, w: w, h: h };
                }
            }
            var canvases = document.querySelectorAll("canvas");
            for (var j = 0; j < canvases.length; j++) {
                var c = canvases[j];
                var cw = c.width || c.offsetWidth || 0;
                var ch = c.height || c.offsetHeight || 0;
                if (cw >= 160 && ch >= 160) {
                    return { index: j, w: cw, h: ch, isCanvas: true };
                }
            }
            return null;
        }""")
        if result:
            if result.get("isCanvas"):
                loc = page.locator("canvas").nth(result["index"])
            else:
                loc = page.locator("img").nth(result["index"])
            bbox = await loc.bounding_box()
            if bbox and bbox["width"] >= 150 and bbox["height"] >= 150:
                screenshot = await loc.screenshot(type="png")
                b64 = base64.b64encode(screenshot).decode()
                logger.info(f"[Douyin] QR captured via fallback - {bbox['width']:.0f}x{bbox['height']:.0f}px")
                return b64
    except Exception as e:
        logger.warning(f"[Douyin] QR fallback error: {e}")
    return None

async def _capture_qr_via_network(page):
    """通过拦截网络响应获取二维码图片 + 监听 check_qrconnect 扫码状态。

    合并 QR 码捕获和扫码状态监听，从页面创建时就开始监听，
    避免后续注册监听器时 CDP 阻塞导致漏监听。

    返回 (captured_dict, on_response, qr_event, qrconnect_status)
    - captured_dict: QR码相关数据
    - on_response: 监听器函数引用，用于后续移除
    - qr_event: QR码捕获完成事件
    - qrconnect_status: check_qrconnect 状态字典（实时更新）
    """
    captured = {"b64": None, "token": None, "qrcode_url": None, "captured_at": None}
    qr_event = asyncio.Event()
    qrconnect_status = {"status": None, "redirect_url": None, "updated": False}

    async def on_response(response):
        url = response.url
        url_lower = url.lower()

        # --- get_qrcode 响应：捕获 QR 码 ---
        if "get_qrcode" in url_lower:
            if captured["b64"]:
                return
            logger.debug(f"[Douyin] QR response intercepted: status={response.status}, url={url[:100]}")
            try:
                data = await response.json()
                qr_data = data.get("data", {}) if isinstance(data, dict) else {}

                has_token = bool(qr_data.get("token"))
                has_qr = bool(qr_data.get("qrcode") or qr_data.get("qrcode_index_url") or qr_data.get("image"))

                if not has_token or not has_qr:
                    err_code = qr_data.get("error_code", -1)
                    err_msg = qr_data.get("description", "") or qr_data.get("error_msg", "")
                    logger.info(f"[Douyin] get_qrcode not usable: error_code={err_code}, error_msg={err_msg}, has_token={has_token}, has_qr={has_qr}")
                    return

                if qr_data.get("token") and not captured["token"]:
                    captured["token"] = qr_data["token"]
                    logger.info(f"[Douyin] QR token captured: {captured['token'][:20]}...")

                # 尝试从多个字段获取 base64
                b64 = (qr_data.get("qrcode")
                       or qr_data.get("image")
                       or qr_data.get("qrcode_image_base64")
                       or qr_data.get("image_base64"))
                if b64:
                    if "," in b64:
                        b64 = b64.split(",", 1)[1]
                    if not _validate_qr_image(b64):
                        logger.debug(f"[Douyin] QR network b64 failed validation, skipping")
                    else:
                        captured["b64"] = b64
                        captured["captured_at"] = time.monotonic()
                        logger.info(f"[Douyin] QR captured via network (base64) from {url[:80]}")
                        qr_event.set()
                        return

                # 尝试从 URL 下载 QR 码图片
                qr_url = (qr_data.get("qrcode_index_url")
                          or qr_data.get("qrcode_url")
                          or qr_data.get("image_url"))
                if qr_url:
                    captured["qrcode_url"] = qr_url
                    try:
                        async with httpx.AsyncClient(timeout=10, trust_env=True) as c:
                            r = await c.get(qr_url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"})
                            if r.status_code == 200 and r.content:
                                dl_b64 = base64.b64encode(r.content).decode()
                                if _validate_qr_image(dl_b64):
                                    captured["b64"] = dl_b64
                                    captured["captured_at"] = time.monotonic()
                                    logger.info(f"[Douyin] QR captured via network (url-download) from {url[:80]}")
                                    qr_event.set()
                                else:
                                    logger.debug(f"[Douyin] QR url-download failed validation, size={len(r.content)}")
                    except Exception as dl_err:
                        logger.debug(f"[Douyin] QR url download error: {dl_err}")
            except Exception as e:
                logger.debug(f"[Douyin] QR network intercept parse error: {e}")
            return

        # --- check_qrconnect 响应：捕获扫码状态 ---
        if "check_qrconnect" in url_lower:
            try:
                data = await response.json()
                qr_data = data.get("data", {}) if isinstance(data, dict) else {}
                err_code = qr_data.get("error_code", -1)
                if err_code != 0:
                    logger.debug(f"[Douyin] check_qrconnect error_code={err_code}, desc={qr_data.get('description','')}")
                    return
                status = str(qr_data.get("status", "1"))
                redirect_url = qr_data.get("redirect_url")
                # 从 URL 中提取 token 用于调试
                url_token = ""
                try:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(response.url)
                    url_token = parse_qs(parsed.query).get("token", [""])[0]
                except Exception:
                    pass
                old_status = qrconnect_status.get("status")
                qrconnect_status["status"] = status
                qrconnect_status["redirect_url"] = redirect_url
                qrconnect_status["updated"] = True
                qrconnect_status["token"] = url_token
                # 状态变化时打印详细日志
                if status != old_status:
                    logger.info(
                        f"[Douyin] check_qrconnect status change: {old_status} -> {status}, "
                        f"redirect={'yes' if redirect_url else 'no'}, "
                        f"token={url_token[:12]}..." if url_token else f"token=none"
                    )
                else:
                    logger.debug(f"[Douyin] check_qrconnect same status={status}, token={url_token[:12]}..." if url_token else f"[Douyin] check_qrconnect same status={status}")
            except Exception as e:
                logger.debug(f"[Douyin] check_qrconnect parse error: {e}")

    page.on("response", on_response)
    return captured, on_response, qr_event, qrconnect_status


async def _fetch_qr_via_sso_api(page):
    """通过 Playwright page.request API 调用抖音 SSO API 获取二维码。

    使用 page.request 而非 page.evaluate 的原因：
    - page.evaluate 在抖音重 JS 页面上经常超时（需等待页面空闲）
    - page.request 直接发送 HTTP 请求，但复用浏览器的 cookies、UA、上下文
    - 速度更快，不依赖页面 JS 执行环境

    返回 (qr_base64, sso_token) 或 (None, None)。
    """
    try:
        sso_url = (
            "https://login.douyin.com/passport/web/get_qrcode/?"
            "passport_jssdk_version=3.1.3&aid=2906"
            "&service=https%3A%2F%2Fwww.douyin.com&mask=1"
        )

        # 使用 Playwright 的 context.request API，直接通过浏览器上下文发送请求，
        # 不依赖页面状态（page.request 可能因为页面JS太重而阻塞）
        resp = await asyncio.wait_for(
            page.context.request.get(sso_url, headers={
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
            }),
            timeout=30
        )

        data = await resp.json()
        logger.info(f"[Douyin] SSO API response keys: {list(data.keys())[:20]}")
        logger.info(f"[Douyin] SSO API response sample: {str(data)[:500]}")

        # 抖音 SSO API 响应格式：
        # 成功: {"data": {"token": "...", "image": "...", ...}, "message": "success"}
        # 失败: {"data": {"error_code": 4031, "description": "..."}, "message": "error"}
        if data.get("message") != "success" or not data.get("data"):
            err_desc = data.get("data", {}).get("description", "") if data.get("data") else ""
            logger.warning(f"[Douyin] SSO API failed: message={data.get('message')}, desc={err_desc}")
            return None, None

        qr_data = data["data"]
        sso_token = qr_data.get("token")
        if not sso_token:
            logger.warning("[Douyin] SSO API returned no token")
            return None, None

        # 路径1：直接返回 base64 图片
        if qr_data.get("image"):
            img_b64 = qr_data["image"]
            if "," in img_b64:
                img_b64 = img_b64.split(",", 1)[1]
            if _validate_qr_image(img_b64):
                logger.info(f"[Douyin] QR captured via SSO API (base64, page.request), token={sso_token[:16]}...")
                return img_b64, sso_token
            else:
                logger.debug(f"[Douyin] SSO API base64 image failed validation")

        # 路径2：返回 URL，用 page.request 下载图片
        if qr_data.get("qrcode_index_url"):
            qr_url = qr_data["qrcode_index_url"]
            try:
                img_resp = await page.context.request.get(qr_url)
                if img_resp.status == 200:
                    img_bytes = await img_resp.body()
                    if len(img_bytes) >= 300:
                        img_b64 = base64.b64encode(img_bytes).decode()
                        if _validate_qr_image(img_b64):
                            logger.info(f"[Douyin] QR captured via SSO API (url-download, context.request), token={sso_token[:16]}...")
                            return img_b64, sso_token
                        else:
                            logger.debug(f"[Douyin] SSO API url-download image failed validation, size={len(img_bytes)}")
            except Exception as dl_err:
                logger.debug(f"[Douyin] SSO QR url download error: {dl_err}")

        logger.warning("[Douyin] SSO API returned no valid image data")
        return None, sso_token
    except Exception as e:
        logger.warning(f"[Douyin] SSO API fetch exception: {type(e).__name__}: {e}")
        logger.exception("[Douyin] SSO API fetch traceback")
        return None, None


async def _extract_qr_from_dom(page):
    """从 DOM 中提取已渲染的二维码图片 base64。

    抖音登录页渲染二维码的方式可能有多种：
    1. <img src="data:image/png;base64,..."> - 内联base64
    2. <img src="https://..."> - 普通URL图片
    3. <canvas> - canvas渲染
    4. background-image CSS样式

    此函数尝试所有可能的方式提取 QR 码，返回 (b64, token) 或 (None, None)。
    token 留空由调用方兜底。
    """
    try:
        # 快速路径：使用 query_selector 直接查找 QR 码容器内的 img
        # 比 page.evaluate 快得多，不会在重JS页面上超时
        qr_selectors = [
            '#animate_qrcode_conta img',
            '[id*="qrcode" i] img',
            '[class*="qrcode" i] img',
            '[class*="qr-code" i] img',
        ]
        for sel in qr_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    src = await el.get_attribute("src")
                    if src and src.startswith("data:image"):
                        idx = src.find(",")
                        b64 = src[idx + 1:] if idx > 0 else src
                        if len(b64) > 100 and _validate_qr_image(b64):
                            logger.info(f"[Douyin] QR captured via DOM fast-path (selector={sel}, b64 len={len(b64)})")
                            return b64, None
                    elif src and src.startswith("http"):
                        # URL 图片，下载
                        try:
                            img_resp = await page.context.request.get(src)
                            if img_resp.status == 200:
                                img_bytes = await img_resp.body()
                                if len(img_bytes) >= 300:
                                    img_b64 = base64.b64encode(img_bytes).decode()
                                    if _validate_qr_image(img_b64):
                                        logger.info(f"[Douyin] QR captured via DOM fast-path (url-download, size={len(img_bytes)})")
                                        return img_b64, None
                        except Exception:
                            pass
            except Exception:
                continue

        # 慢速路径：使用 page.evaluate 全面扫描（可能超时，限时 6s）
        try:
            result = await asyncio.wait_for(page.evaluate("""() => {
            // 优先在已知 QR 容器内查找
            const containers = document.querySelectorAll(
                '[id*="qrcode"],[id*="qr_code"],[id*="qrCode"],[id*="QR"],'
                + '[class*="qrcode"],[class*="qr-code"],[class*="qr_code"],[class*="QrCode"],'
                + '[class*="web-login"],[class*="login-modal"],[class*="login-container"],'
                + '[class*="scan-box"],[class*="scan-code"],[class*="login-qrcode"],'
                + '[class*="qr-container"],[class*="qr-wrap"],[class*="qrcode-box"]'
            );
            let bestUrl = null;
            let bestSize = 0;

            // 扫描 img 标签
            const scanImgs = (root) => {
                if (!root) return;
                root.querySelectorAll('img').forEach(im => {
                    const src = im.src || '';
                    if (!src) return;
                    // 计算图片大小（用于估算质量）
                    const w = im.naturalWidth || im.width || 0;
                    const h = im.naturalHeight || im.height || 0;
                    const area = w * h;
                    // 优先选择包含 qrcode 的 URL，或者面积大的
                    if (src.indexOf('qrcode') >= 0 || src.indexOf('qr_code') >= 0) {
                        if (area > bestSize) {
                            bestSize = area;
                            bestUrl = src;
                        }
                    } else if (w >= 100 && h >= 100 && area > bestSize) {
                        // 容器内的大图也可能是 QR 码
                        bestSize = area;
                        bestUrl = src;
                    }
                });
            };

            // 扫描 canvas
            const scanCanvas = (root) => {
                if (!root) return;
                root.querySelectorAll('canvas').forEach(cv => {
                    const r = cv.getBoundingClientRect();
                    if (r.width >= 100 && r.height >= 100) {
                        try {
                            const dataUrl = cv.toDataURL('image/png');
                            const idx = dataUrl.indexOf(',');
                            if (idx > 0 && dataUrl.length - idx > 500) {
                                bestUrl = dataUrl;
                                bestSize = 999999;
                            }
                        } catch (e) {}
                    }
                });
            };

            // 扫描 background-image
            const scanBgImg = (root) => {
                if (!root) return;
                const all = root.querySelectorAll('*');
                all.forEach(el => {
                    const style = window.getComputedStyle(el);
                    const bg = style.backgroundImage || '';
                    if (bg.indexOf('url(') >= 0 && (bg.indexOf('qrcode') >= 0 || bg.indexOf('qr') >= 0)) {
                        const match = bg.match(/url\\(['"]?([^'")]+)['"]?\\)/);
                        if (match && match[1]) {
                            const w = parseInt(style.width) || 0;
                            const h = parseInt(style.height) || 0;
                            const area = w * h;
                            if (area > bestSize) {
                                bestSize = area;
                                bestUrl = match[1];
                            }
                        }
                    }
                });
            };

            containers.forEach(c => {
                scanImgs(c);
                scanCanvas(c);
            });

            // 容器内未找到，全局扫描
            if (!bestUrl) {
                scanImgs(document);
                scanCanvas(document);
            }

            return bestUrl;
        }"""), timeout=6)
        except asyncio.TimeoutError:
            logger.warning("[Douyin] DOM slow-path evaluate timed out (6s)")
            return None, None

        if not result:
            return None, None

        # 如果是 data:image 格式，直接提取 base64
        if result.startswith("data:image"):
            idx = result.find(',')
            if idx > 0:
                b64 = result[idx + 1:]
                if len(b64) > 100 and _validate_qr_image(b64):
                    logger.info(f"[Douyin] QR captured via DOM extraction (data:image, b64 len={len(b64)})")
                    return b64, None
            return None, None

        # 如果是普通 URL，下载图片
        if result.startswith("http"):
            try:
                img_resp = await page.context.request.get(result)
                if img_resp.status == 200:
                    img_bytes = await img_resp.body()
                    if len(img_bytes) >= 300:
                        img_b64 = base64.b64encode(img_bytes).decode()
                        if _validate_qr_image(img_b64):
                            logger.info(f"[Douyin] QR captured via DOM extraction (url-download, size={len(img_bytes)})")
                            return img_b64, None
                        else:
                            logger.debug(f"[Douyin] DOM url-download failed validation, size={len(img_bytes)}")
            except Exception as dl_err:
                logger.debug(f"[Douyin] DOM QR url download error: {dl_err}")

        return None, None
    except Exception as e:
        logger.debug(f"[Douyin] DOM QR extraction error: {e}")
        return None, None


async def _poll_sso_status(page, sso_token):
    """监听页面自身发起的 check_qrconnect 响应来获取扫码状态。

    抖音页面 JS SDK 会自动轮询 check_qrconnect（带完整签名参数），
    我们监听其响应即可，不能主动 fetch（缺少签名会被 4031 拦截）。

    返回 dict: { status, redirect_url } 或 None（无新响应时）。
    status 映射:
      "1" → waiting (等待扫码)
      "2" → scanned (已扫码，待确认)
      "3" → confirmed (已确认，redirect_url 有值)
      "4" → expired (已过期)
    """
    # 此函数不再主动 fetch，改为由 _poll 中的 response 监听器捕获
    # 保留函数签名以兼容现有调用，实际逻辑在 _poll 中通过 on_response 实现
    return None


async def _active_check_qrconnect(page, sso_token: str, verbose: bool = False, ms_token: str = "") -> dict:
    """主动调用 check_qrconnect API 检查扫码状态（被动监听的补充）。

    被动监听依赖页面 JS SDK 发起 check_qrconnect 请求，但登录弹窗关闭或
    页面导航后 JS 可能停止轮询，导致永远收不到 confirmed 状态。
    此函数通过 page.context.request 主动发起请求（复用浏览器 cookie 上下文），
    作为被动监听的补充。

    参数 verbose=True 时输出 info 级别日志（用于 scanned 状态后的关键轮询）。
    ms_token: 从外部传入的 msToken cookie 值，避免在函数内调用 page.context.cookies 阻塞 CDP。
    返回 dict: {"status": "1"|"2"|"3"|"4", "redirect_url": str|None} 或 None。
    """
    if not sso_token:
        return None
    log = logger.info if verbose else logger.debug
    try:
        url = (
            "https://login.douyin.com/passport/web/check_qrconnect/?"
            f"token={sso_token}&passport_jssdk_version=3.1.3&aid=2906"
            "&service=https%3A%2F%2Fwww.douyin.com&mask=1"
        )
        if ms_token:
            url += f"&msToken={ms_token}"

        resp = await asyncio.wait_for(
            page.context.request.get(url, headers={
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
            }),
            timeout=8
        )
        data = await resp.json()
        qr_data = data.get("data", {}) if isinstance(data, dict) else {}
        err_code = qr_data.get("error_code", -1)
        if err_code != 0:
            log(f"[Douyin] active check_qrconnect error_code={err_code}, description={qr_data.get('description','')}")
            return None
        status = str(qr_data.get("status", "1"))
        redirect_url = qr_data.get("redirect_url")
        log(f"[Douyin] active check_qrconnect: status={status}, redirect={'yes' if redirect_url else 'no'}")
        return {"status": status, "redirect_url": redirect_url}
    except asyncio.TimeoutError:
        log("[Douyin] active check_qrconnect timed out (8s)")
        return None
    except Exception as e:
        log(f"[Douyin] active check_qrconnect failed: {type(e).__name__}: {e}")
        return None


async def _active_check_qrconnect_via_page(page, sso_token: str, verbose: bool = False) -> dict:
    """通过页面上下文（page.evaluate）调用 check_qrconnect 接口。

    直接用 page.context.request 调用会被抖音风控拦截（error_code=4031），
    因为缺少签名参数。而在页面上下文中用 fetch 调用会自动携带正确的
    cookie、referer 以及 JS SDK 可能注入的签名逻辑。

    返回 dict: {"status": "1"|"2"|"3"|"4", "redirect_url": str|None} 或 None。
    """
    if not sso_token:
        return None
    log = logger.info if verbose else logger.debug
    try:
        result = await asyncio.wait_for(
            page.evaluate("""async (token) => {
                try {
                    const url = 'https://login.douyin.com/passport/web/check_qrconnect/?' +
                        'token=' + encodeURIComponent(token) +
                        '&passport_jssdk_version=3.1.3&aid=2906' +
                        '&service=https%3A%2F%2Fwww.douyin.com&mask=1';
                    const resp = await fetch(url, {
                        method: 'GET',
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json, text/plain, */*',
                        }
                    });
                    const data = await resp.json();
                    const qrData = data.data || {};
                    return {
                        error_code: qrData.error_code ?? -1,
                        status: String(qrData.status ?? '1'),
                        redirect_url: qrData.redirect_url || null,
                        description: qrData.description || ''
                    };
                } catch(e) {
                    return { error: e.message };
                }
            }""",
            sso_token
            ),
            timeout=10
        )
        if isinstance(result, dict) and result.get("error"):
            log(f"[Douyin] page-eval check_qrconnect error: {result['error']}")
            return None
        if isinstance(result, dict) and result.get("error_code", -1) != 0:
            log(f"[Douyin] page-eval check_qrconnect error_code={result.get('error_code')}, description={result.get('description','')}")
            return None
        if isinstance(result, dict):
            status = str(result.get("status", "1"))
            redirect_url = result.get("redirect_url")
            log(f"[Douyin] page-eval check_qrconnect: status={status}, redirect={'yes' if redirect_url else 'no'}")
            return {"status": status, "redirect_url": redirect_url}
        return None
    except asyncio.TimeoutError:
        log("[Douyin] page-eval check_qrconnect timed out (10s)")
        return None
    except Exception as e:
        log(f"[Douyin] page-eval check_qrconnect failed: {type(e).__name__}: {e}")
        return None


async def _setup_qrconnect_listener(page, session_key):
    """设置 check_qrconnect 响应监听器，捕获页面自身发起的轮询响应。

    返回 (latest_status_dict, on_response) 便于调用方后续移除监听器。
    latest_status_dict 会被监听器实时更新，_poll 读取其内容即可。
    """
    latest = {"status": None, "redirect_url": None, "updated": False}

    async def on_response(response):
        url = response.url
        if "check_qrconnect" not in url.lower():
            return
        try:
            data = await response.json()
            qr_data = data.get("data", {}) if isinstance(data, dict) else {}
            err_code = qr_data.get("error_code", -1)
            if err_code != 0:
                return
            status = str(qr_data.get("status", "1"))
            redirect_url = qr_data.get("redirect_url")
            latest["status"] = status
            latest["redirect_url"] = redirect_url
            latest["updated"] = True
        except Exception:
            pass

    page.on("response", on_response)
    return latest, on_response


async def _inject_qr_poller(page, sso_token: str):
    """在页面上注入状态检测器，被动劫持SDK请求 + 多路径兜底检测。

    重要：不主动发起 check_qrconnect 请求（会被抖音风控拦截，error_code=4031）。
    检测策略（按优先级）：
    1. 劫持 SDK 的 fetch/XHR，被动捕获 check_qrconnect 响应（主路径）
    2. 直接检查 document.cookie 中是否有登录态 cookie（最可靠兜底）
    3. 检查 DOM 中是否有用户头像/昵称等登录后元素
    4. 检查 window.location 是否发生变化（登录跳转）
    5. 检查 window.__INITIAL_STATE__.user.isLogin

    结果存储在 window.__douyinQrStatus，Python 端通过 page.evaluate 读取。
    返回 True 表示注入成功。
    """
    if not sso_token:
        return False
    try:
        await page.evaluate("""(token) => {
            if (window.__douyinQrPollerStarted) return;
            window.__douyinQrPollerStarted = true;

            // 伪造页面可见状态，防止抖音 SDK 因 headless 模式下页面不可见而停止轮询
            // SDK 检测 document.hidden=true 时可能降低轮询频率或完全停止轮询
            try {
                Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
                Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
                Object.defineProperty(document, 'webkitHidden', { get: () => false, configurable: true });
                Object.defineProperty(document, 'webkitVisibilityState', { get: () => 'visible', configurable: true });
                // 阻止 visibilitychange 事件传播，防止 SDK 监听到页面不可见
                document.addEventListener('visibilitychange', function(e) {
                    e.stopImmediatePropagation();
                }, true);
            } catch(e) {}

            window.__douyinQrStatus = {
                qrStatus: null,         // 归一化后的状态: waiting/scanned/confirmed/expired
                rawStatus: null,        // SDK 返回的原始状态值
                redirectUrl: null,      // 重定向 URL
                hasLoginCookie: false,  // 是否检测到登录 cookie
                hasUserAvatar: false,   // 是否检测到用户头像
                urlChanged: false,      // URL 是否变化
                currentUrl: location.href,
                lastUpdate: Date.now(),
                checkCount: 0,
                sdkCheckCount: 0,       // SDK 发起的 check_qrconnect 次数
                lastSdkCheckTime: 0,    // 最后一次 SDK check 的时间戳
                sdkStopped: false,      // SDK 是否已停止轮询
                events: []              // 捕获到的事件日志
            };

            function logEvent(msg) {
                const ts = new Date().toISOString();
                window.__douyinQrStatus.events.push(ts + ': ' + msg);
                if (window.__douyinQrStatus.events.length > 100) {
                    window.__douyinQrStatus.events.shift();
                }
                console.log('[QR_MONITOR]', msg);
            }

            // 归一化状态值：抖音可能返回数字字符串或文本字符串
            // 已知可能的值: "1"/"new"/"wait" = 等待扫码, "2"/"scanned" = 已扫码,
            //              "3"/"confirmed"/"success" = 已确认, "4"/"expired"/"timeout" = 已过期
            function normalizeStatus(rawStatus) {
                if (!rawStatus) return null;
                const s = String(rawStatus).toLowerCase();
                if (s === '1' || s === 'new' || s === 'wait' || s === 'waiting') return 'waiting';
                if (s === '2' || s === 'scanned' || s === 'scaned') return 'scanned';
                if (s === '3' || s === 'confirmed' || s === 'success' || s === 'done') return 'confirmed';
                if (s === '4' || s === 'expired' || s === 'timeout' || s === 'invalid') return 'expired';
                return rawStatus; // 未知值原样返回
            }

            function handleQrStatusUpdate(qrData, source) {
                const rawStatus = String(qrData.status || '');
                const normStatus = normalizeStatus(rawStatus);
                const oldNorm = window.__douyinQrStatus.qrStatus;

                window.__douyinQrStatus.rawStatus = rawStatus;
                window.__douyinQrStatus.qrStatus = normStatus;
                window.__douyinQrStatus.redirectUrl = qrData.redirect_url || null;
                window.__douyinQrStatus.sdkCheckCount++;
                window.__douyinQrStatus.lastSdkCheckTime = Date.now();

                if (normStatus !== oldNorm) {
                    logEvent(source + ': ' + (oldNorm || 'null') + ' -> ' + normStatus +
                        ' (raw=' + rawStatus + ')' +
                        (qrData.redirect_url ? ' +redirect' : ''));
                }

                if (normStatus === 'confirmed' && qrData.redirect_url) {
                    logEvent('CONFIRMED via ' + source + ', redirect: ' + qrData.redirect_url.substring(0, 80));
                }
            }

            // ============================================
            // 劫持 fetch，被动捕获 SDK 发起的 check_qrconnect
            // ============================================
            const originalFetch = window.fetch;
            window.fetch = async function(...args) {
                const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
                const response = await originalFetch.apply(this, args);
                if (url.includes('check_qrconnect')) {
                    try {
                        const clone = response.clone();
                        const data = await clone.json();
                        const qrData = data.data || {};
                        if (qrData.error_code === 0 || qrData.status !== undefined) {
                            handleQrStatusUpdate(qrData, 'SDK fetch');
                        }
                    } catch(e) {
                        // 静默忽略解析错误
                    }
                }
                return response;
            };

            // 劫持 XMLHttpRequest
            const OriginalXHR = window.XMLHttpRequest;
            window.XMLHttpRequest = function() {
                const xhr = new OriginalXHR();
                let _url = '';
                const origOpen = xhr.open;
                xhr.open = function(method, url) {
                    _url = url;
                    return origOpen.apply(this, arguments);
                };
                const origSend = xhr.send;
                xhr.send = function() {
                    const args = arguments;
                    if (_url && _url.includes('check_qrconnect')) {
                        xhr.addEventListener('load', function() {
                            try {
                                const data = JSON.parse(xhr.responseText);
                                const qrData = data.data || {};
                                if (qrData.error_code === 0 || qrData.status !== undefined) {
                                    handleQrStatusUpdate(qrData, 'SDK XHR');
                                }
                            } catch(e) {}
                        });
                    }
                    return origSend.apply(this, args);
                };
                return xhr;
            };

            // ============================================
            // Cookie / DOM / URL 检测（兜底方案）
            // ============================================
            function checkLoginState() {
                try {
                    window.__douyinQrStatus.checkCount++;
                    const now = Date.now();
                    window.__douyinQrStatus.lastUpdate = now;
                    const currentNormStatus = window.__douyinQrStatus.qrStatus;

                    // 检测 SDK 是否停止轮询（超过10秒没有 SDK 请求）
                    if (window.__douyinQrStatus.sdkCheckCount > 0 &&
                        now - window.__douyinQrStatus.lastSdkCheckTime > 10000 &&
                        !window.__douyinQrStatus.sdkStopped) {
                        window.__douyinQrStatus.sdkStopped = true;
                        logEvent('SDK polling stopped (no request for >10s), status=' + (currentNormStatus || 'null'));
                    }

                    // 1. 检查登录 cookie（最可靠的兜底方案）
                    const cookies = document.cookie;
                    const hasSessionIdSs = /(?:^|;\\s*)sessionid_ss=/.test(cookies);
                    const hasSessionId = /(?:^|;\\s*)sessionid=/.test(cookies);
                    const hasSidTt = /(?:^|;\\s*)sid_tt=/.test(cookies);
                    const hasUidTt = /(?:^|;\\s*)uid_tt=/.test(cookies);
                    const hasSidGuard = /(?:^|;\\s*)sid_guard=/.test(cookies);

                    const wasLoggedIn = window.__douyinQrStatus.hasLoginCookie;
                    const nowLoggedIn = hasSessionIdSs || (hasSidTt && hasUidTt) || hasSessionId;
                    window.__douyinQrStatus.hasLoginCookie = nowLoggedIn;

                    if (nowLoggedIn && !wasLoggedIn) {
                        logEvent('LOGIN COOKIE DETECTED! sid_ss=' + hasSessionIdSs +
                            ', sessionid=' + hasSessionId +
                            ', sid_tt=' + hasSidTt + ', uid_tt=' + hasUidTt +
                            ', sid_guard=' + hasSidGuard);
                        if (currentNormStatus !== 'confirmed') {
                            window.__douyinQrStatus.qrStatus = 'confirmed';
                            logEvent('CONFIRMED via cookie detection');
                        }
                    }

                    // 2. 检查 URL 变化
                    if (location.href !== window.__douyinQrStatus.currentUrl) {
                        window.__douyinQrStatus.urlChanged = true;
                        logEvent('URL changed: ' +
                            (window.__douyinQrStatus.currentUrl || '').substring(0, 60) +
                            ' -> ' + location.href.substring(0, 60));
                        window.__douyinQrStatus.currentUrl = location.href;
                        if (location.hostname === 'www.douyin.com' &&
                            !location.search.includes('modal_id=login') &&
                            nowLoggedIn) {
                            if (currentNormStatus !== 'confirmed') {
                                window.__douyinQrStatus.qrStatus = 'confirmed';
                                logEvent('CONFIRMED via URL change + login cookie');
                            }
                        }
                    }

                    // 3. 检查 DOM 中的用户头像
                    try {
                        const avatarSelectors = [
                            '[data-e2e="user-avatar"]',
                            '.user-avatar',
                            '.header-user-avatar',
                            'img[src*="p3-pc.douyinpic.com"][src*="avatar"]',
                            '[class*="Avatar"][class*="user"]',
                            '.login-btn + [class*="avatar"]',
                        ];
                        let foundAvatar = false;
                        for (const sel of avatarSelectors) {
                            const el = document.querySelector(sel);
                            if (el && el.offsetParent !== null) {
                                foundAvatar = true;
                                break;
                            }
                        }
                        if (foundAvatar && nowLoggedIn) {
                            window.__douyinQrStatus.hasUserAvatar = true;
                            if (currentNormStatus !== 'confirmed') {
                                window.__douyinQrStatus.qrStatus = 'confirmed';
                                logEvent('CONFIRMED via avatar + login cookie');
                            }
                        }
                    } catch(e) {}

                    // 4. 检查 __INITIAL_STATE__
                    try {
                        if (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user) {
                            const user = window.__INITIAL_STATE__.user;
                            if (user.userInfo && user.userInfo.uid && user.isLogin) {
                                window.__douyinQrStatus.hasLoginCookie = true;
                                if (currentNormStatus !== 'confirmed') {
                                    window.__douyinQrStatus.qrStatus = 'confirmed';
                                    logEvent('CONFIRMED via __INITIAL_STATE__.user.isLogin');
                                }
                            }
                        }
                    } catch(e) {}

                    // 5. 检查页面文本中的登录成功提示
                    try {
                        const bodyText = document.body ? document.body.innerText : '';
                        if (bodyText && (bodyText.includes('登录成功') || bodyText.includes('欢迎回来'))) {
                            if (nowLoggedIn && currentNormStatus !== 'confirmed') {
                                window.__douyinQrStatus.qrStatus = 'confirmed';
                                logEvent('CONFIRMED via body text (login success)');
                            }
                        }
                    } catch(e) {}

                } catch(e) {
                    logEvent('checkLoginState error: ' + e.message);
                }
            }

            // 定期检查（1秒一次）
            setInterval(checkLoginState, 1000);
            // 立即执行一次
            setTimeout(checkLoginState, 300);
            logEvent('QR monitor injected OK, token=' + token.substring(0, 12) +
                '..., SDK passive mode + cookie fallback');
        }""",
            sso_token
        )
        logger.info(f"[Douyin] JS QR monitor injected (token={sso_token[:12]}...)")
        return True
    except Exception as e:
        logger.warning(f"[Douyin] Failed to inject JS QR monitor: {e}")
        return False


async def _read_qr_poller_status(page) -> dict:
    """读取页面上 JS 状态检测器的最新结果。

    返回 dict，包含 qrStatus/redirectUrl/hasLoginCookie/hasUserAvatar/urlChanged/events/sdkCheckCount/ourCheckCount 等。
    """
    try:
        result = await page.evaluate("""() => {
            const s = window.__douyinQrStatus;
            if (!s) return null;
            return {
                qrStatus: s.qrStatus,
                redirectUrl: s.redirectUrl,
                hasLoginCookie: s.hasLoginCookie || false,
                hasUserAvatar: s.hasUserAvatar || false,
                urlChanged: s.urlChanged || false,
                currentUrl: s.currentUrl,
                checkCount: s.checkCount || 0,
                sdkCheckCount: s.sdkCheckCount || 0,
                ourCheckCount: s.ourCheckCount || 0,
                lastUpdate: s.lastUpdate || 0,
                events: (s.events || []).slice(-15)  // 最近15条事件
            };
        }""")
        return result or {}
    except Exception as e:
        logger.debug(f"[Douyin] Read QR monitor status failed: {e}")
        return {}


async def _capture_qr_fullpage_crop(page):
    """全页截图后裁剪登录弹窗区域（DOM 元素截图的降级方案）。

    当登录弹窗存在但内部 img/canvas 无法直接截图时，截取弹窗容器区域。
    """
    try:
        info = await page.evaluate("""() => {
            var modal = document.querySelector('[class*="login" i] [class*="container" i]')
                     || document.querySelector('[class*="login" i]')
                     || document.querySelector('[class*="modal" i][class*="web" i]')
                     || document.querySelector('[class*="qrcode" i]')
                     || document.querySelector('[class*="qr" i][class*="code" i]');
            if (!modal) return null;
            var r = modal.getBoundingClientRect();
            if (r.width < 200 || r.height < 200) return null;
            return { x: r.x, y: r.y, w: r.width, h: r.height };
        }""")
        if info and info["w"] >= 200 and info["h"] >= 200:
            clip = {"x": info["x"], "y": info["y"], "width": info["w"], "height": info["h"]}
            screenshot = await page.screenshot(type="png", clip=clip)
            b64 = base64.b64encode(screenshot).decode()
            logger.info(f"[Douyin] QR captured via fullpage crop: {info['w']:.0f}x{info['h']:.0f}px")
            return b64
    except Exception as e:
        logger.debug(f"[Douyin] fullpage crop error: {e}")
    return None


async def _extract_token(page):
    """Extract auth token from page state / cookies.

    page.evaluate 在抖音重 JS 页面上可能阻塞数十秒，加 5 秒超时保护。
    token 非关键路径（sso_token 已由网络拦截提供），超时则跳过。
    """
    token = None
    try:
        token = await asyncio.wait_for(page.evaluate("""() => {
            var s = window.__INITIAL_STATE__ || {};
            if (s.token) return s.token;
            var p = new URLSearchParams(window.location.search);
            var t = p.get("token");
            if (t) return t;
            var sc = document.querySelectorAll("script");
            for (var i = 0; i < sc.length; i++) {
                var m = sc[i].textContent.match(/"token"\\s*:\\s*"([^"]+)"/);
                if (m) return m[1];
            }
            return null;
        }"""), timeout=5)
    except asyncio.TimeoutError:
        logger.debug("[Douyin] token extraction via evaluate timed out (5s), skipping")
    except Exception as e:
        logger.debug(f"[Douyin] token extraction via script failed: {e}")
    if not token:
        try:
            for c in await asyncio.wait_for(page.context.cookies(), timeout=5):
                if "token" in c.get("name","").lower():
                    token = c["value"]
                    break
        except asyncio.TimeoutError:
            logger.debug("[Douyin] token extraction via cookies timed out (5s)")
        except Exception as e:
            logger.debug(f"[Douyin] token extraction via cookies failed: {e}")
    if token:
        logger.debug("[Douyin] Token acquired")
    return token

# ---- Polling ----
_POLL_DEADLINE_SEC = 240  # 增加到 240s，给二次验证足够时间
_POLL_INTERVAL_SEC = 2

# Douyin auth cookies: these appear/change after successful QR scan login
_DOUYIN_AUTH_COOKIES = [
    "sessionid_ss",      # main session cookie (appears after login)
    "sid_guard",          # session guard (appears after login)
    "uid_tt",             # user id (appears/updates after login)
    "sid_tt",             # session token (appears after login)
    "passport_csrf_token",# CSRF token (updates after login)
]
# Cookies that exist pre-login and whose value change indicates scan/auth
_DOUYIN_PRELOGIN_COOKIES = [
    "odin_tt",            # device fingerprint (value changes on auth)
    "passport_csrf_token_default",
]

# 二次验证检测关键词
_VERIFY_KEYWORDS = [
    "安全验证", "身份验证", "验证码", "滑块",
    "拖动滑块", "请完成验证", "security_verify",
    "请输入验证码", "短信验证", "人脸识别",
]
# 二次验证相关的 URL 关键词
_VERIFY_URL_KEYWORDS = ["security", "verify", "captcha", "check"]

# 可见浏览器验证轮询超时（秒）：给用户足够时间完成验证
_VERIFY_BROWSER_TIMEOUT_SEC = 120
# 可见浏览器轮询间隔（秒）
_VERIFY_POLL_INTERVAL_SEC = 3


async def _detect_verification_needed(page) -> bool:
    """检测页面是否出现二次验证。

    通过 URL 和页面文本内容判断是否需要二次验证。
    """
    try:
        current_url = page.url or ""
        for kw in _VERIFY_URL_KEYWORDS:
            if kw in current_url.lower():
                logger.info(f"[Douyin] Verify detected via URL keyword: {kw} in {current_url[:80]}")
                return True
    except Exception:
        pass
    try:
        body = await asyncio.wait_for(
            page.evaluate("document.body ? document.body.innerText : ''"),
            timeout=5,
        )
        for kw in _VERIFY_KEYWORDS:
            if kw in body:
                logger.info(f"[Douyin] Verify detected via body keyword: {kw}")
                return True
    except (asyncio.TimeoutError, Exception):
        pass
    return False


async def _bring_page_to_front(page) -> bool:
    """将浏览器窗口从屏幕外移到可见区域并全屏，用于二次验证。

    浏览器启动时窗口定位在屏幕外（-32000,-32000），用户不可见。
    扫码后检测到需要二次验证时调用此函数，通过 CDP 将窗口移到屏幕可见区域并最大化。
    因为用户已扫码，页面会直接停留在二次验证界面，不需要重新扫码。

    返回 True 表示成功，False 表示失败。
    """
    try:
        client = await page.context.new_cdp_session(page)
        # 获取当前页面对应的窗口 ID
        window_info = await client.send("Browser.getWindowForTarget")
        window_id = window_info.get("windowId")
        if window_id is None:
            logger.warning("[Douyin] CDP: no windowId returned")
            await client.detach()
            return False
        # 先将窗口从屏幕外移到可见区域，再最大化
        await client.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {
                "left": 0,
                "top": 0,
                "width": 800,
                "height": 600,
                "windowState": "normal",
            }
        })
        # 窗口最大化（全屏显示）
        await client.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {
                "windowState": "maximized",
            }
        })
        await client.detach()
        # 激活标签页，确保窗口在最前面
        try:
            await page.bring_to_front()
        except Exception:
            pass
        logger.info("[Douyin] Browser window maximized for verification")
        return True
    except Exception as e:
        logger.warning(f"[Douyin] Failed to bring page to front: {type(e).__name__}: {e}")
        return False


async def _launch_visible_browser_for_verify(source_ctx, target_url: str):
    """启动可见浏览器用于二次验证。

    从 source_ctx 复制所有 cookie 到新的可见浏览器上下文，
    导航到 target_url（通常为抖音主页），让用户在可见窗口中完成验证。

    返回 (playwright, browser, context, page) 或 None（启动失败）。
    """
    from playwright.async_api import async_playwright

    pw = None
    browser = None
    ctx = None
    page = None
    try:
        # 收集源上下文的所有 cookie
        source_cookies = await _collect_all_cookies(source_ctx)
        logger.info(f"[Douyin] Copying {len(source_cookies)} cookies to visible browser")

        pw = await asyncio.wait_for(async_playwright().start(), timeout=15)
        browser = await asyncio.wait_for(
            pw.chromium.launch(
                headless=False,  # 可见模式，用户可交互
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--window-size=480,720",
                ],
            ),
            timeout=20,
        )
        ctx = await browser.new_context(
            viewport={"width": 480, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        # 复制 cookie 到新上下文（完整复制，包括 expires 等属性）
        if source_cookies:
            cookie_list = []
            for c in source_cookies:
                cookie = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".douyin.com"),
                    "path": c.get("path", "/"),
                }
                # 复制 expires（过期时间），确保 session cookie 和持久 cookie 都正确继承
                expires = c.get("expires")
                if expires and expires > 0:
                    cookie["expires"] = expires
                if c.get("httpOnly"):
                    cookie["httpOnly"] = True
                if c.get("secure"):
                    cookie["secure"] = True
                if c.get("sameSite"):
                    ss = c["sameSite"]
                    if ss in ("Strict", "Lax", "None"):
                        cookie["sameSite"] = ss
                cookie_list.append(cookie)
            await ctx.add_cookies(cookie_list)
            logger.info(f"[Douyin] Copied {len(cookie_list)} cookies to visible browser context")

        page = await ctx.new_page()
        # 导航到目标 URL，让页面展示验证或登录状态
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.debug(f"[Douyin] Visible browser goto tolerated: {e}")

        logger.info("[Douyin] Visible browser launched for verification")
        return pw, browser, ctx, page
    except Exception as e:
        logger.error(f"[Douyin] Failed to launch visible browser: {e}")
        # 清理已创建的资源
        try:
            if ctx:
                await ctx.close()
            if browser:
                await browser.close()
            if pw:
                await pw.stop()
        except Exception:
            pass
        return None


async def _poll_visible_browser(ctx, session_key: str, cancel_event: asyncio.Event) -> Optional[list]:
    """轮询可见浏览器的 cookie，检测登录成功。

    成功时返回 cookie 列表，超时或取消时返回 None。
    """
    deadline = time.monotonic() + _VERIFY_BROWSER_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if cancel_event.is_set():
            logger.info(f"[Douyin] Visible browser poll cancelled for {session_key[:8]}")
            return None
        if session_key not in _login_sessions:
            return None
        st = _login_sessions[session_key]
        if st.get("status") in ("confirmed", "expired", "error"):
            return None
        try:
            current_cookies = await asyncio.wait_for(ctx.cookies(), timeout=5)
            current_map = {c["name"]: c["value"] for c in current_cookies}
            sessionid_ss = current_map.get("sessionid_ss", "")
            sid_tt = current_map.get("sid_tt", "")
            uid_tt = current_map.get("uid_tt", "")

            if sessionid_ss and len(sessionid_ss) > 10:
                logger.info(f"[Douyin] Visible browser: CONFIRMED via sessionid_ss")
                return current_cookies
            if sid_tt and uid_tt and len(sid_tt) > 10:
                logger.info(f"[Douyin] Visible browser: CONFIRMED via sid_tt+uid_tt")
                return current_cookies
        except asyncio.TimeoutError:
            logger.debug("[Douyin] Visible browser cookie fetch timed out")
        except Exception as e:
            logger.debug(f"[Douyin] Visible browser cookie fetch error: {e}")
        await asyncio.sleep(_VERIFY_POLL_INTERVAL_SEC)

    logger.warning(f"[Douyin] Visible browser poll timed out ({_VERIFY_BROWSER_TIMEOUT_SEC}s)")
    return None


async def _poll(session_key: str, page, cancel_event: asyncio.Event = None):
    """轮询抖音扫码状态，支持取消。

    多层检测策略（都不主动调用会被风控拦截的 check_qrconnect API）：
    1. JS monitor（页面注入）：劫持 fetch/XHR 被动捕获 SDK 请求 + 检查 cookie/DOM/URL
    2. response 监听器：拦截 SDK 发起的 check_qrconnect 网络响应
    3. 直接 cookie 检查：每次轮询都直接获取所有 cookie，检查关键登录 cookie
    4. DOM 检测：Python 端检查页面上的用户头像等元素
    5. URL 变化检测：检测页面是否跳转到登录后的页面
    """
    if cancel_event is None:
        cancel_event = asyncio.Event()
    elapsed = 0
    session = _login_sessions.get(session_key, {})
    baseline = session.get("baseline_cookies", {})
    sso_token = session.get("sso_token")
    initial_url = page.url
    qrconnect_status = session.get("qrconnect_status", {"status": None, "redirect_url": None, "updated": False})
    logger.info(
        f"[Douyin] Poll start | baseline cookies: {len(baseline)} | "
        f"url={initial_url[:60]} | qrconnect_status={qrconnect_status.get('status')}"
    )

    # 注册 framenavigated 监听，检测页面跳转
    nav_detected = {"url": None}
    def on_framenavigated(frame):
        if frame == page.main_frame:
            nav_detected["url"] = frame.url
            logger.info(f"[Douyin] Page navigated to: {frame.url[:100]}")
    page.on("framenavigated", on_framenavigated)

    try:
        for _ in range(int(_POLL_DEADLINE_SEC / _POLL_INTERVAL_SEC)):
            if cancel_event.is_set():
                logger.info(f"[Douyin] Poll cancelled for {session_key[:8]}")
                return
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            elapsed += _POLL_INTERVAL_SEC
            if session_key not in _login_sessions:
                return
            st = _login_sessions[session_key]
            if st.get("status") in ("confirmed", "expired", "error"):
                return

            try:
                # ===== 1. JS Monitor 状态（最高优先级）=====
                poller_status = await _read_qr_poller_status(page)
                if poller_status:
                    p_qr_status = str(poller_status.get("qrStatus") or "")
                    p_redirect = poller_status.get("redirectUrl")
                    p_has_cookie = poller_status.get("hasLoginCookie", False)
                    p_has_avatar = poller_status.get("hasUserAvatar", False)
                    p_url_changed = poller_status.get("urlChanged", False)
                    p_count = poller_status.get("checkCount", 0)
                    p_sdk_count = poller_status.get("sdkCheckCount", 0)
                    p_our_count = poller_status.get("ourCheckCount", 0)
                    p_events = poller_status.get("events", [])

                    # 首次读取时记录完整日志，确认 JS monitor 在工作
                    if not st.get("_monitor_logged") and p_count > 0:
                        st["_monitor_logged"] = True
                        logger.info(
                            f"[Douyin] JS monitor active (checkCount={p_count}, "
                            f"sdkCheckCount={p_sdk_count}, ourCheckCount={p_our_count})"
                        )
                        if p_events:
                            for evt in p_events[-5:]:
                                logger.info(f"[Douyin] JS monitor event: {evt}")

                    # 状态变化时输出完整事件日志
                    if p_qr_status and p_qr_status != st.get("_last_monitor_status"):
                        st["_last_monitor_status"] = p_qr_status
                        logger.info(
                            f"[Douyin] JS monitor: status={p_qr_status}, "
                            f"sdkChecks={p_sdk_count}, ourChecks={p_our_count}, "
                            f"redirect={'yes' if p_redirect else 'no'}"
                        )
                        if p_events:
                            for evt in p_events[-8:]:
                                logger.info(f"[Douyin] JS monitor event: {evt}")

                    # 每30秒输出一次诊断日志（即使状态没变化），便于排查问题
                    if elapsed > 0 and elapsed % 30 == 0 and p_count > 0:
                        logger.info(
                            f"[Douyin] Poll t={elapsed}s | status={st.get('status')} | "
                            f"js_qrStatus={p_qr_status or 'null'} | "
                            f"js_hasCookie={p_has_cookie} | "
                            f"js_sdkChecks={p_sdk_count} | js_ourChecks={p_our_count} | "
                            f"js_checkCount={p_count}"
                        )
                        if p_events:
                            for evt in p_events[-5:]:
                                logger.debug(f"[Douyin] JS monitor event: {evt}")

                    # JS monitor 检测到 confirmed 状态（通过 fetch/XHR 劫持或 cookie/DOM 检查）
                    if p_qr_status in ("3", "confirmed", "success") or p_has_cookie:
                        logger.info(
                            f"[Douyin] JS monitor: CONFIRMED at t={elapsed}s "
                            f"(qrStatus={p_qr_status}, hasCookie={p_has_cookie}, "
                            f"hasAvatar={p_has_avatar}, urlChanged={p_url_changed})"
                        )
                        if p_events:
                            for evt in p_events[-5:]:
                                logger.info(f"[Douyin] JS monitor event: {evt}")
                        if p_redirect:
                            try:
                                await page.goto(p_redirect, wait_until="domcontentloaded", timeout=15000)
                                await asyncio.sleep(2)
                            except Exception as nav_err:
                                logger.warning(f"[Douyin] Redirect navigation error: {nav_err}")
                        # 收集 cookie（跨 domain 完整收集）
                        try:
                            all_cks = await _collect_all_cookies(page.context)
                            st["cookie_str"] = _cookies_to_str(all_cks)
                            logger.info(
                                f"[Douyin] Collected {len(all_cks)} cookies "
                                f"via JS monitor confirmed path"
                            )
                        except Exception:
                            pass
                        try:
                            st["user_info"] = await _collect_login_user_info(page, st.get("cookie_str", ""))
                        except Exception as e:
                            logger.warning(f"[Douyin] User extraction error: {e}")
                            uid_ck = _extract_uid_from_cookies(st.get("cookie_str", ""))
                            st["user_info"] = {"uid": uid_ck, "nickname": "抖音用户", "avatar": ""}
                        st["status"] = "confirmed"
                        _invalidate_qr_cache()
                        st["message"] = "登录成功"
                        return

                    # JS monitor 检测到 expired
                    if p_qr_status in ("4", "expired", "timeout"):
                        st["status"] = "expired"
                        _invalidate_qr_cache()
                        st["message"] = "二维码已过期"
                        logger.info(f"[Douyin] JS monitor: expired at t={elapsed}s")
                        return

                    # JS monitor 检测到 scanned
                    if p_qr_status in ("2", "scanned"):
                        if st.get("status") != "scanned":
                            st["status"] = "scanned"
                            st["message"] = "已扫码，请在手机上确认"
                            st["_scanned_at"] = time.monotonic()  # 记录扫码时间
                            logger.info(f"[Douyin] JS monitor: scanned at t={elapsed}s")

                    # JS monitor 检测到 URL 变化但没 cookie，记录一下
                    if p_url_changed and not st.get("_url_change_logged"):
                        st["_url_change_logged"] = True
                        logger.info(f"[Douyin] JS monitor: URL changed (not logged in yet)")

                # ===== 2. response 监听器状态 =====
                if qrconnect_status["updated"]:
                    qrconnect_status["updated"] = False
                    status_code = str(qrconnect_status.get("status", "new"))
                    is_expired = status_code in ("4", "expired", "timeout")
                    is_scanned = status_code in ("2", "scanned")
                    is_confirmed = status_code in ("3", "confirmed", "success")

                    if is_expired:
                        st["status"] = "expired"
                        _invalidate_qr_cache()
                        st["message"] = "二维码已过期"
                        logger.info(f"[Douyin] QRconnect: expired at t={elapsed}s (status={status_code})")
                        return
                    elif is_scanned:
                        if st.get("status") != "scanned":
                            st["status"] = "scanned"
                            st["message"] = "已扫码，请在手机上确认"
                            st["_scanned_at"] = time.monotonic()
                            logger.info(f"[Douyin] QRconnect: scanned at t={elapsed}s (status={status_code})")
                    elif is_confirmed:
                        redirect_url = qrconnect_status.get("redirect_url")
                        logger.info(
                            f"[Douyin] QRconnect: CONFIRMED at t={elapsed}s "
                            f"(status={status_code}, redirect={'yes' if redirect_url else 'no'})"
                        )
                        if redirect_url:
                            try:
                                await page.goto(redirect_url, wait_until="domcontentloaded", timeout=15000)
                                await asyncio.sleep(2)
                            except Exception as nav_err:
                                logger.warning(f"[Douyin] Redirect navigation error: {nav_err}")
                        try:
                            all_cks = await _collect_all_cookies(page.context)
                            st["cookie_str"] = _cookies_to_str(all_cks)
                            logger.info(
                                f"[Douyin] Collected {len(all_cks)} cookies "
                                f"via qrconnect confirmed path"
                            )
                        except Exception:
                            pass
                        try:
                            st["user_info"] = await _collect_login_user_info(page, st.get("cookie_str", ""))
                        except Exception as e:
                            logger.warning(f"[Douyin] User extraction error: {e}")
                            uid_ck = _extract_uid_from_cookies(st.get("cookie_str", ""))
                            st["user_info"] = {"uid": uid_ck, "nickname": "抖音用户", "avatar": ""}
                        st["status"] = "confirmed"
                        _invalidate_qr_cache()
                        st["message"] = "登录成功"
                        return
                    # 其他状态（"new"/"1"/waiting）：继续等待
                    logger.debug(f"[Douyin] QRconnect: waiting status={status_code} at t={elapsed}s")

                # ===== 3. 直接 Cookie 检查（核心检测路径）=====
                # 不主动调用 check_qrconnect API（会被风控拦截 error_code=4031）
                # 直接检查关键登录 cookie 是否存在，这是最可靠的检测方式
                detected = "waiting"
                try:
                    current_cookies = await asyncio.wait_for(page.context.cookies(), timeout=5)
                except asyncio.TimeoutError:
                    logger.debug(f"[Douyin] cookie fetch timed out at t={elapsed}s")
                    continue
                except Exception as e:
                    logger.debug(f"[Douyin] cookie fetch error: {e}")
                    continue
                current_map = {c["name"]: c["value"] for c in current_cookies}

                # 诊断：每 15 秒输出一次 cookie 名称列表，便于排查登录态
                if elapsed > 0 and elapsed % 15 == 0:
                    auth_names = [n for n in current_map if n in _DOUYIN_AUTH_COOKIES]
                    all_names = sorted(current_map.keys())
                    logger.info(
                        f"[Douyin] Cookie diag t={elapsed}s | total={len(current_map)} | "
                        f"auth_keys={auth_names} | "
                        f"sid_ss={'yes' if current_map.get('sessionid_ss') else 'no'} | "
                        f"sid_tt={'yes' if current_map.get('sid_tt') else 'no'} | "
                        f"sid_guard={'yes' if current_map.get('sid_guard') else 'no'} | "
                        f"passport_csrf={'yes' if current_map.get('passport_csrf_token') else 'no'}"
                    )

                # 直接检查核心登录 cookie（不依赖 baseline 对比，只要存在且长度足够就算登录成功）
                sessionid_ss = current_map.get("sessionid_ss", "")
                sid_tt = current_map.get("sid_tt", "")
                sessionid = current_map.get("sessionid", "")
                uid_tt = current_map.get("uid_tt", "")

                # 强信号：sessionid_ss 存在且长度足够
                if sessionid_ss and len(sessionid_ss) > 10:
                    detected = "confirmed"
                    logger.info(
                        f"[Douyin] CONFIRMED via sessionid_ss cookie at t={elapsed}s "
                        f"(sid_ss_len={len(sessionid_ss)}, sid_tt={'yes' if sid_tt else 'no'})"
                    )
                # 强信号：sid_tt + uid_tt 同时存在
                elif sid_tt and uid_tt and len(sid_tt) > 10:
                    detected = "confirmed"
                    logger.info(f"[Douyin] CONFIRMED via sid_tt+uid_tt cookies at t={elapsed}s")
                # 弱信号：scanned 状态检测（通过 prelogin cookie 变化）
                elif st.get("status") == "waiting":
                    csrf_now = current_map.get("passport_csrf_token", "")
                    csrf_base = baseline.get("passport_csrf_token", "")
                    odin_now = current_map.get("odin_tt", "")
                    odin_base = baseline.get("odin_tt", "")
                    if (csrf_now and csrf_base and csrf_now != csrf_base) or \
                       (odin_now and odin_base and odin_now != odin_base):
                        detected = "scanned"
                        logger.info(
                            f"[Douyin] SCANNED via prelogin cookie change at t={elapsed}s"
                        )

                # --- URL 变化检测（scanned 状态后补充）---
                # 登录确认后抖音页面通常会跳转，URL 会从 login.douyin.com 变为 www.douyin.com
                if detected == "waiting" and st.get("status") == "scanned":
                    try:
                        current_url = page.url
                        initial_url = st.get("_initial_url", initial_url)
                        # 保存初始 URL（首次检测时）
                        if "_initial_url" not in st:
                            st["_initial_url"] = current_url
                        # 如果 URL 域名从登录域变为抖音主页域，说明登录成功
                        if "login.douyin.com" in initial_url and "www.douyin.com" in current_url \
                                and current_url != initial_url:
                            detected = "confirmed"
                            logger.info(
                                f"[Douyin] CONFIRMED via URL change | "
                                f"{initial_url[:60]} -> {current_url[:60]}"
                            )
                    except Exception:
                        pass

                # Fallback: body text / DOM check (only used if cookie delta has no signal)
                if detected == "waiting":
                    try:
                        body = await asyncio.wait_for(
                            page.evaluate("document.body ? document.body.innerText : ''"),
                            timeout=5
                        )
                        if "登录成功" in body:
                            detected = "confirmed"
                            logger.info("[Douyin] Confirmed via body text fallback")
                        elif "已扫码" in body or "已扫描" in body or "请在手机上确认" in body:
                            if st.get("status") != "scanned":
                                detected = "scanned"
                    except asyncio.TimeoutError:
                        logger.debug(f"[Douyin] body text check timed out at t={elapsed}s")
                    except Exception:
                        pass

                # 额外 DOM 检测：scanned 状态后检查是否存在登录后的用户头像元素
                if detected == "waiting" and st.get("status") == "scanned" and elapsed > 15:
                    try:
                        has_avatar = await asyncio.wait_for(
                            page.evaluate("""() => {
                                // 检测抖音主页的用户头像元素
                                const selectors = [
                                    '.user-avatar', '.avatar', '[data-e2e="user-avatar"]',
                                    '.header-user-avatar', '.nav-user-avatar'
                                ];
                                for (const sel of selectors) {
                                    const el = document.querySelector(sel);
                                    if (el && el.offsetParent !== null) return true;
                                }
                                // 检测 __INITIAL_STATE__ 中的用户信息
                                const win = window;
                                if (win.__INITIAL_STATE__ && win.__INITIAL_STATE__.user) {
                                    const user = win.__INITIAL_STATE__.user;
                                    if (user.uid || user.user_id) return true;
                                }
                                return false;
                            }"""),
                            timeout=5
                        )
                        if has_avatar:
                            detected = "confirmed"
                            logger.info("[Douyin] Confirmed via DOM avatar detection")
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        pass

                # 主动探测：scanned 状态持续 20 秒以上仍未 confirmed 时，
                # 主动导航到抖音主页检查登录态（应对页面 JS SDK 停止轮询的情况）
                if detected == "waiting" and st.get("status") == "scanned" and elapsed > 20:
                    probe_count = st.get("_login_probe_count", 0)
                    # 每 15 秒探测一次，避免过于频繁
                    if probe_count == 0 or (elapsed - st.get("_last_probe_time", 0)) >= 15:
                        st["_login_probe_count"] = probe_count + 1
                        st["_last_probe_time"] = elapsed
                        logger.info(f"[Douyin] Login probe #{probe_count + 1} at t={elapsed}s: navigating to douyin.com to check login state")
                        try:
                            # 保存当前 URL，探测失败后可以返回
                            probe_start_url = page.url
                            await page.goto("https://www.douyin.com/",
                                          wait_until="domcontentloaded", timeout=15000)
                            await asyncio.sleep(2)
                            # 检查 cookie 中是否有 sessionid_ss
                            probe_cookies = await asyncio.wait_for(page.context.cookies(), timeout=5)
                            probe_cookie_map = {c["name"]: c["value"] for c in probe_cookies}
                            probe_session = probe_cookie_map.get("sessionid_ss", "")
                            if probe_session:
                                detected = "confirmed"
                                logger.info(
                                    f"[Douyin] CONFIRMED via login probe | sessionid_ss found, "
                                    f"total auth cookies: {len([c for c in _DOUYIN_AUTH_COOKIES if probe_cookie_map.get(c)])}"
                                )
                            else:
                                logger.info(f"[Douyin] Login probe: no sessionid_ss found, returning to login page")
                                # 没有检测到登录态，返回登录页面
                                try:
                                    await page.goto(probe_start_url, wait_until="domcontentloaded", timeout=10000)
                                except Exception:
                                    pass
                        except Exception as probe_err:
                            logger.warning(f"[Douyin] Login probe failed: {type(probe_err).__name__}: {probe_err}")

                # ===== 4. 二次验证检测已移除 =====
                # 浏览器窗口始终可见，用户扫码后如遇二次验证可直接在浏览器中完成。
                # 主轮询循环持续检测 cookie，用户完成验证后 cookie 会被设置，自动确认登录。

                # Expired check
                if elapsed > 30:
                    try:
                        body = await asyncio.wait_for(
                            page.evaluate("document.body ? document.body.innerText : ''"),
                            timeout=5
                        )
                        if "已过期" in body or "已失效" in body or "二维码过期" in body:
                            st["status"] = "expired"
                            _invalidate_qr_cache()
                            st["message"] = "二维码已过期"
                            return
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        pass

                # --- STATE TRANSITIONS ---
                if detected == "scanned" and st.get("status") != "scanned":
                    st["status"] = "scanned"
                    st["message"] = "已扫码，请在手机上确认"
                    st["_scanned_at"] = time.monotonic()
                    logger.info(f"[Douyin] Cookie delta: scanned at t={elapsed}s")
                elif detected == "confirmed":
                    # Collect all cookies（跨 domain 完整收集）
                    try:
                        all_cks = await _collect_all_cookies(page.context)
                        st["cookie_str"] = _cookies_to_str(all_cks)
                        logger.info(
                            f"[Douyin] Collected {len(all_cks)} cookies "
                            f"via cookie-delta confirmed path"
                        )
                    except asyncio.TimeoutError:
                        logger.warning("[Douyin] cookie collection timed out in delta confirmed path")
                    except Exception as e:
                        logger.debug(f"[Douyin] cookie collection failed: {e}")
                    # 提取用户信息（统一走 _collect_login_user_info，与 check_qrconnect 路径一致）
                    try:
                        st["user_info"] = await _collect_login_user_info(page, st.get("cookie_str", ""))
                    except Exception as e:
                        logger.warning(f"[Douyin] User extraction error: {e}")
                        uid_ck = _extract_uid_from_cookies(st.get("cookie_str", ""))
                        st["user_info"] = {"uid": uid_ck, "nickname": "抖音用户", "avatar": ""}

                    # SET STATUS AFTER user_info is fully populated (avoid race condition)
                    st["status"] = "confirmed"
                    _invalidate_qr_cache()
                    st["message"] = "登录成功"
                    return
            except (PlaywrightError, httpx.HTTPError, asyncio.TimeoutError) as e:
                # 预期异常（浏览器/网络瞬时故障）：记录后继续轮询，避免单次抖动终止登录流程
                logger.warning(f"[Douyin] Poll transient error at t={elapsed}s: {type(e).__name__}: {e}")
                continue
            except Exception as e:
                # 非预期异常（KeyError/AttributeError/逻辑错误等）：终止轮询并通知用户
                logger.error(f"[Douyin] Poll fatal error at t={elapsed}s: {type(e).__name__}: {e}", exc_info=True)
                if session_key in _login_sessions:
                    _login_sessions[session_key]["status"] = "error"
                    _invalidate_qr_cache()
                    _login_sessions[session_key]["message"] = "登录过程异常，请重试"
                return

        if session_key in _login_sessions:
            _login_sessions[session_key]["status"] = "expired"
            _invalidate_qr_cache()
            _login_sessions[session_key]["message"] = "二维码已过期"
            # Schedule cleanup in 30s to close browser context
            _spawn_background_task(_delayed_cleanup(session_key, 30))
    finally:
        # 注意：不移除 response 监听器
        # 监听器在页面创建时就注册了，页面销毁时会自动清理。
        # 如果手动移除可能会影响其他正在使用该页面的操作。
        pass

async def _extract_user_info_from_page(page):
    """Extract Douyin user info from the current page using JS evaluation.

    抖音页面 JS 极重，page.evaluate 可能长时间阻塞。用 asyncio.wait_for 兜底，
    超时 8 秒后返回空 dict，由调用方走 Cookie 兜底路径。
    """
    try:
        return await asyncio.wait_for(page.evaluate("""() => {
        var nick = "", uid = "", av = "";
        var s = window.__INITIAL_STATE__;
        if (!s) s = {};

        // Path 1: userInfo.user (profile pages)
        var u = (s.userInfo && s.userInfo.user) || s.user || s.userInfo
             || s.userProfile || s.profile || s.currentUser || s.loginUser || {};

        nick = u.nickname || u.nick_name || u.name || u.uniqueId
            || u.unique_id || u.shortId || u.short_id || "";
        uid = u.uid || u.id_str || u.id || u.userId || u.user_id
           || u.sec_uid || u.short_id || "";
        av = u.avatar_thumb || u.avatar_168x168 || u.avatar_medium
          || u.avatar_larger || u.avatar_300x300 || u.avatar || u.avatar_url || "";

        // Path 2: root-level user keys
        if (!nick) {
            var keys = ["userInfo", "user", "profile", "authorInfo", "creator"];
            for (var i = 0; i < keys.length; i++) {
                var v = s[keys[i]];
                if (v) {
                    nick = v.nickname || v.name || v.uniqueId || "";
                    uid = uid || v.uid || v.id || "";
                    if (nick) break;
                }
            }
        }

        // Path 3: localStorage
        if (!nick) {
            try {
                var lsKeys = ["userInfo", "user_info", "USER_INFO", "douyin_user",
                             "dy_user_info", "account_info"];
                for (var j = 0; j < lsKeys.length; j++) {
                    var raw = localStorage.getItem(lsKeys[j]);
                    if (raw) {
                        try {
                            var ls = JSON.parse(raw);
                            nick = ls.nickname || ls.nick_name || ls.name
                                || ls.uniqueId || ls.nickName || "";
                            uid = uid || ls.uid || ls.id || ls.userId || ls.user_id || "";
                            av = av || ls.avatar || ls.avatar_url || ls.avatarUrl || "";
                            if (nick) break;
                        } catch(e2) {}
                    }
                }
            } catch(e) {}
        }

        // Path 4: sessionStorage
        if (!nick) {
            try {
                var ssKeys = ["userInfo", "user"];
                for (var k = 0; k < ssKeys.length; k++) {
                    var raw2 = sessionStorage.getItem(ssKeys[k]);
                    if (raw2) {
                        try {
                            var ss = JSON.parse(raw2);
                            nick = ss.nickname || ss.name || "";
                            uid = uid || ss.uid || ss.id || "";
                            if (nick) break;
                        } catch(e3) {}
                    }
                }
            } catch(e) {}
        }

        // Path 5: meta + title (low confidence, only as last resort)
        if (!nick) {
            var metas = document.querySelectorAll(
                'meta[name="keywords"], meta[property="og:title"], meta[name="author"]'
            );
            for (var m = 0; m < metas.length; m++) {
                var c = metas[m].content || "";
                if (c && c.indexOf(",") > 0) {
                    nick = c.split(",")[0].trim();
                    if (nick) break;
                }
                if (c && c.indexOf("抖音") < 0 && c.length > 1 && c.length < 30) {
                    nick = c.trim(); break;
                }
            }
        }

        return {uid: uid, nickname: nick, avatar: av};
    }"""), timeout=8)
    except asyncio.TimeoutError:
        logger.warning("[Douyin] User info DOM extraction timed out (8s)")
        return {}
    except Exception as e:
        logger.debug(f"[Douyin] User info DOM extraction error: {e}")
        return {}


async def _fetch_user_info_via_api(cookie_str: str) -> dict:
    """Use Douyin API to get user info with authenticated cookies."""
    try:
        import httpx
        headers = {
            "User-Agent": USER_AGENT,
            "Cookie": cookie_str,
            "Referer": "https://www.douyin.com/",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            # Try user profile API
            r = await client.get(
                "https://www.douyin.com/aweme/v1/web/user/profile/self/",
                headers=headers,
                params={"device_platform": "webapp", "aid": "6383"}
            )
            if r.status_code == 200:
                data = r.json()
                user = data.get("user", {})
                if user.get("nickname"):
                    return {
                        "uid": user.get("uid", user.get("short_id", "")),
                        "nickname": user.get("nickname", ""),
                        "avatar": user.get("avatar_thumb", {}).get("url_list", [""])[0]
                               or user.get("avatar_medium", {}).get("url_list", [""])[0]
                               or "",
                    }
    except Exception as e:
        logger.debug(f"[Douyin] API user fetch failed: {e}")
    return {}

async def _collect_login_user_info(page, cookie_str: str) -> dict:
    """登录确认后统一收集用户信息（3 级回退：API → DOM → Cookie）。

    抽取自 _poll 中 check_qrconnect 与 cookie delta 两条 confirmed 路径的重复逻辑，
    统一为一份实现，确保两条路径行为一致：
      Stage 1: 调用抖音 API（带 cookie，最可靠）
      Stage 2: 导航到 douyin.com 从 DOM/__INITIAL_STATE__ 提取
      Stage 3: 从 cookie 中提取 uid/nickname 兜底

    返回 {"uid","nickname","avatar"}，nickname 永不为空（兜底"抖音用户"）。
    """
    ud = {}
    # Stage 1: API（带超时保护，避免网络问题卡住整个登录流程）
    if not ud.get("nickname"):
        logger.info("[Douyin] Trying API for user info...")
        try:
            api_ud = await asyncio.wait_for(
                _fetch_user_info_via_api(cookie_str), timeout=8
            )
            if api_ud.get("nickname"):
                ud = api_ud
                logger.info(f"[Douyin] User from API: {ud['nickname']}")
        except asyncio.TimeoutError:
            logger.warning("[Douyin] API user info timed out (8s)")
        except Exception as e:
            logger.debug(f"[Douyin] API user info exception: {e}")
    # Stage 2: DOM（带超时保护，避免页面加载慢卡住）
    if not ud.get("nickname"):
        logger.info("[Douyin] Navigating to douyin.com for user info...")
        try:
            async with asyncio.timeout(15):
                await page.goto("https://www.douyin.com/",
                              wait_until="domcontentloaded", timeout=12000)
                await asyncio.sleep(3)
                dom_ud = await _extract_user_info_from_page(page)
                if dom_ud and dom_ud.get("nickname"):
                    ud = dom_ud
                    logger.info(f"[Douyin] User from home page: {ud['nickname']}")
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("[Douyin] DOM user info timed out (15s)")
        except Exception as nav_err:
            logger.warning(f"[Douyin] Home page nav failed: {nav_err}")
    # Stage 3: Cookie fallback（保留已提取的 uid/avatar）
    if not ud.get("nickname"):
        uid_ck = _extract_uid_from_cookies(cookie_str)
        nick_ck = _extract_nickname_from_cookies(cookie_str)
        ud = {
            "uid": ud.get("uid") or uid_ck,
            "nickname": nick_ck or "抖音用户",
            "avatar": ud.get("avatar", ""),
        }
    logger.info(f"[Douyin] Final user: {ud.get('nickname','?')} (uid={ud.get('uid','?')})")
    return ud

def _extract_nickname_from_cookies(cookie_str: str) -> str:
    """Extract nickname from Douyin-specific cookie fields."""
    if not cookie_str:
        return ""
    import urllib.parse
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, val = part.partition("=")
            name_l = name.strip().lower()
            val = val.strip()
            # Direct nickname cookies
            if name_l in ("nickname", "nick_name", "user_name", "douyin_nickname"):
                return urllib.parse.unquote(val)
            # Try URL-encoded nickname in certain cookie values
            if name_l in ("odin_tt",) and "%" in val:
                try:
                    urllib.parse.unquote(val)
                    # odin_tt sometimes contains user info fragments
                except Exception:
                    pass
    return ""

def _extract_uid_from_cookies(cookie_str: str) -> str:
    """Extract uid from Douyin cookie string."""
    if not cookie_str:
        return ""
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, val = part.partition("=")
            name_l = name.strip().lower()
            # Douyin-specific uid cookie names
            if name_l in ("uid_tt", "douyin_uid", "dy_uid", "uid", "user_id"):
                return val.strip()
    # Try partial match
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part and "uid" in part.split("=")[0].strip().lower():
            return part.split("=")[1].strip()
    return ""


async def _cleanup(key: str):
    _invalidate_qr_cache()
    st = _login_sessions.pop(key, None)
    if not st:
        return
    # Cancel any running poll task and wait for it to exit
    cancel_ev = st.get("_cancel_event")
    if cancel_ev:
        cancel_ev.set()
        await asyncio.sleep(0.3)  # brief yield for poll to see cancellation
    page = st.get("page")
    ctx = st.get("context")
    try:
        if page and not page.is_closed():
            await page.close()
    except Exception as e:
        logger.debug(f"[Douyin] page.close error: {e}")
    try:
        if ctx:
            await ctx.close()
    except Exception as e:
        logger.debug(f"[Douyin] ctx.close error: {e}")

async def _delayed_cleanup(key: str, delay: int):
    """Delayed cleanup to close browser context after poll expires."""
    await asyncio.sleep(delay)
    await _cleanup(key)


# 兜底 GC：定期扫描 _login_sessions，清理超时未结束的会话
# 防御任何遗漏路径（如 _delayed_cleanup 任务被取消、_safe_poll 之外的其他异常）
_SESSION_GC_INTERVAL_SEC = 300  # 每 5 分钟扫描一次
_SESSION_GC_MAX_AGE_SEC = 600   # 超过 10 分钟的非 confirmed 会话视为僵尸
_session_gc_task: Optional[asyncio.Task] = None

# 预热池 QR 码主动刷新：每 10s 检查一次，QR 码超过 40s 就重新点击刷新
# 刷新时不清除旧 QR 码，等新的捕获到了再替换（原子替换）
# 40s 阈值：QR 码有效期 60s，提前 20s 开始刷新，确保新 QR 码生成时旧的还没过期
# 同一时间只刷新一个页面，避免 CDP 竞争导致刷新更慢
_qr_refresh_interval_sec = 10
_qr_refresh_max_age_sec = 40
_qr_refresh_task: Optional[asyncio.Task] = None
# 记录正在刷新的 page id，避免重复刷新
_refreshing_pages = set()


async def _warmup_qr_refresh_loop():
    """后台定期刷新预热池中过期的 QR 码。

    每 10s 检查一次预热池中的页面，QR 码超过 50s 就重新点击登录按钮刷新。
    关键策略：刷新时不清除旧 QR 码，等新的捕获到了再原子替换。
    这样即使刷新需要很长时间（CDP 阻塞 60-180s），旧的 QR 码仍然可用。
    """
    logger.info("[Douyin] Warmup QR refresh loop started")
    while True:
        try:
            await asyncio.sleep(_qr_refresh_interval_sec)
            # 同一时间只刷新一个页面，避免 CDP 竞争
            if _refreshing_pages:
                continue
            # 有用户正在扫码时暂停刷新，避免替换掉用户正在用的 QR token
            active_sessions = [
                k for k, v in _login_sessions.items()
                if v.get("status") in ("waiting", "scanned")
            ]
            if active_sessions:
                logger.debug(
                    f"[Douyin] QR refresh skipped: {len(active_sessions)} active login session(s)"
                )
                continue
            async with _qr_pool_lock:
                entries = list(_qr_page_pool)
            # 按 QR 码年龄排序，先刷新最老的
            entries_with_age = []
            for entry in entries:
                captured = entry.get("captured", {})
                captured_at = captured.get("captured_at")
                page = entry.get("page")
                if not page or page.is_closed():
                    continue
                page_id = id(page)
                if page_id in _refreshing_pages:
                    continue
                if not captured_at:
                    continue
                age = time.monotonic() - captured_at
                if age < _qr_refresh_max_age_sec:
                    continue
                entries_with_age.append((age, entry, page_id))
            if not entries_with_age:
                continue
            # 选最老的那个刷新
            entries_with_age.sort(reverse=True, key=lambda x: x[0])
            age, entry, page_id = entries_with_age[0]
            # 标记为刷新中
            _refreshing_pages.add(page_id)
            logger.info(f"[Douyin] Refreshing stale warmup QR (age={age:.0f}s)")

            async def _do_refresh(entry, page_id):
                try:
                    page = entry.get("page")
                    if not page or page.is_closed():
                        return
                    # 双重检查：确保页面仍然在池中，且没有活跃的登录会话
                    # 避免竞态条件：gen_qr 可能在检查活跃会话之后取走了页面，
                    # 此时导航会清除已注入的 JS monitor 和 SDK 轮询状态
                    async with _qr_pool_lock:
                        still_in_pool = any(
                            e.get("page") is page
                            for e in _qr_page_pool
                        )
                    if not still_in_pool:
                        logger.debug("[Douyin] Refresh aborted: page no longer in pool")
                        return
                    active_sessions = [
                        k for k, v in _login_sessions.items()
                        if v.get("status") in ("waiting", "scanned")
                    ]
                    if active_sessions:
                        logger.debug(
                            f"[Douyin] Refresh aborted: {len(active_sessions)} "
                            f"active session(s) started since check"
                        )
                        return
                    # 创建新的捕获容器
                    new_captured = {"b64": None, "token": None, "captured_at": None}
                    new_qr_event = asyncio.Event()

                    async def on_response(resp):
                        if "get_qrcode" in resp.url and resp.status == 200:
                            try:
                                body = await resp.json()
                                data = body.get("data", {}) if isinstance(body, dict) else {}
                                token = data.get("token", "")
                                qr = data.get("qrcode") or data.get("qrcode_index_url") or ""
                                if token and qr and len(qr) > 100:
                                    if qr.startswith("data:image"):
                                        idx = qr.find(",")
                                        qr = qr[idx + 1:] if idx > 0 else qr
                                    new_captured["b64"] = qr
                                    new_captured["token"] = token
                                    new_captured["captured_at"] = time.monotonic()
                                    new_qr_event.set()
                                    logger.info(f"[Douyin] Warmup QR refresh captured new QR (len={len(qr)})")
                            except Exception:
                                pass

                    page.on("response", on_response)
                    try:
                        # 重新导航到 modal_id=login URL，页面会自动重新加载 QR 码
                        # 不需要点击登录按钮，绕开 CDP 阻塞问题
                        try:
                            await page.goto("https://www.douyin.com/?modal_id=login", wait_until="domcontentloaded", timeout=30000)
                        except Exception as e:
                            logger.debug(f"[Douyin] refresh goto tolerated: {e}")
                        # 等新的 QR 码（最多 60 秒，通常 10-15s）
                        try:
                            await asyncio.wait_for(new_qr_event.wait(), timeout=60)
                            # 原子替换：新 QR 码捕获成功，替换旧的
                            old_captured = entry.get("captured", {})
                            old_captured["b64"] = new_captured["b64"]
                            old_captured["token"] = new_captured["token"]
                            old_captured["captured_at"] = new_captured["captured_at"]
                            if entry.get("qr_event"):
                                entry["qr_event"].set()
                            logger.info("[Douyin] Warmup QR refresh completed successfully")
                        except asyncio.TimeoutError:
                            logger.warning("[Douyin] Warmup QR refresh timed out (60s)")
                    finally:
                        try:
                            page.remove_listener("response", on_response)
                        except Exception:
                            pass
                finally:
                    _refreshing_pages.discard(page_id)

            _spawn_background_task(_do_refresh(entry, page_id))
        except asyncio.CancelledError:
            logger.info("[Douyin] Warmup QR refresh loop cancelled")
            raise
        except Exception as e:
            logger.error(f"[Douyin] Warmup QR refresh loop error: {e}", exc_info=True)


def start_warmup_qr_refresh():
    """启动预热池 QR 刷新后台任务。"""
    global _qr_refresh_task
    if _qr_refresh_task is not None and not _qr_refresh_task.done():
        return
    _qr_refresh_task = _spawn_background_task(_warmup_qr_refresh_loop())


async def stop_warmup_qr_refresh() -> None:
    """停止预热池 QR 刷新后台任务。

    取消刷新任务并等待其退出，避免在 stop_qr_pool 后
    仍有后台任务尝试访问已关闭的页面。
    """
    global _qr_refresh_task
    if _qr_refresh_task is not None:
        _qr_refresh_task.cancel()
        try:
            await asyncio.wait_for(_qr_refresh_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            pass
        _qr_refresh_task = None
    _refreshing_pages.clear()
    logger.info("[Douyin] Warmup QR refresh stopped")


async def _session_gc_loop():
    """后台定期清理僵尸登录会话，避免浏览器上下文泄露。"""
    logger.info("[Douyin] Session GC loop started")
    while True:
        try:
            await asyncio.sleep(_SESSION_GC_INTERVAL_SEC)
            now = time.monotonic()
            stale_keys = []
            for key, st in list(_login_sessions.items()):
                created = st.get("created_at")
                if not created:
                    # 历史数据无 created_at，补一个并跳过本轮
                    st["created_at"] = now
                    continue
                if now - created > _SESSION_GC_MAX_AGE_SEC and st.get("status") not in ("confirmed",):
                    stale_keys.append(key)
            for key in stale_keys:
                logger.warning(f"[Douyin] GC: cleaning stale session {key[:8]} (age>={_SESSION_GC_MAX_AGE_SEC}s)")
                await _cleanup(key)
        except asyncio.CancelledError:
            logger.info("[Douyin] Session GC loop cancelled")
            raise
        except Exception as e:
            # GC 循环自身不能因异常退出，否则永远不再清理
            logger.error(f"[Douyin] Session GC loop error: {e}", exc_info=True)


def start_session_gc():
    """启动会话 GC 后台任务（在应用 lifespan startup 中调用）。"""
    global _session_gc_task
    if _session_gc_task is not None and not _session_gc_task.done():
        return _session_gc_task
    _session_gc_task = _spawn_background_task(_session_gc_loop())
    return _session_gc_task


async def stop_session_gc():
    """停止会话 GC 后台任务（在应用 lifespan shutdown 中调用）。"""
    global _session_gc_task
    if _session_gc_task and not _session_gc_task.done():
        _session_gc_task.cancel()
        try:
            await _session_gc_task
        except asyncio.CancelledError:
            pass
    _session_gc_task = None

async def _resolve_douyin_session() -> tuple[Optional[str], Optional[str]]:
    """从内存缓存或 DB 获取抖音 cookie 和 session_id。

    返回 (cookie_str, session_id)：
      - 完全未登录：(None, None)
      - 仅有 cookie 但无 session_id：(cookie, None)
    """
    from app.routers.auth import login_sessions
    from app.database import get_db_context

    c = login_sessions.get("douyin-active")
    cookie_str = c.get("douyin_cookie") if c else None
    session_id = c.get("session_id") if c else None

    if not cookie_str:
        try:
            async with get_db_context() as db:
                r = await db.execute(
                    select(UserSessionModel)
                    .where(UserSessionModel.douyin_cookie.isnot(None))
                    .where(UserSessionModel.platform == Platform.DOUYIN)
                    .where(UserSessionModel.is_valid == True)  # noqa: E712
                    .order_by(UserSessionModel.created_at.desc())
                    .limit(1)
                )
                s = r.scalar_one_or_none()
                if s and s.douyin_cookie:
                    cookie_str = decrypt_secret(s.douyin_cookie)
                    if not session_id:
                        session_id = s.session_id
        except Exception as e:
            logger.error(f"[Douyin] DB cookie lookup failed: {e}")

    return cookie_str, session_id


async def _upsert_to_folder(
    db: AsyncSession,
    videos: list[dict],
    folder_title: str,
    result_dict: dict,
    user_scope: str,
    synced_ids: Optional[set] = None,
) -> None:
    """upsert 视频到指定收藏夹。

    优化点：批量预加载 VideoCache 与 FavoriteVideo，避免循环内逐条查询（N+1）。
    保留逐条 flush + UNIQUE 异常捕获，保证并发写入冲突不中断整体流程。

    增量同步：当 ``synced_ids`` 非 None 时为增量模式，跳过 "删除旧 folder 重建"
    的全量替换语义，仅追加新增的 FavoriteVideo 行，避免清空已同步数据。
    ``new_count`` 在增量模式下取 "当前 aweme_id 集合 - synced_ids" 的差集大小。

    注意：fetch 返回的视频字段为 ``video_id``，对应 DB 列 ``VideoCache.bvid``。
    """
    if not videos:
        return
    from sqlalchemy import select as sa_select, delete as sa_delete
    from app.models import FavoriteFolder, FavoriteVideo, VideoCache
    from app.services.sync_cursor import diff_aweme_ids

    incremental = synced_ids is not None

    folder = await db.scalar(
        sa_select(FavoriteFolder).where(
            FavoriteFolder.platform == "douyin",
            FavoriteFolder.session_id == f"douyin-{user_scope}-{folder_title}",
            FavoriteFolder.title == folder_title,
        )
    )
    if folder and not incremental:
        # 全量同步：删除旧 folder 重建，避免 ORM cache 问题
        await db.execute(sa_delete(FavoriteVideo).where(FavoriteVideo.folder_id == folder.id))
        await db.delete(folder)
        await db.flush()
        folder = None
    # 增量同步：保留已有 folder（含其 FavoriteVideo 行），仅追加新增
    if not folder:
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

    # 增量模式下 new_count 取差集大小；全量模式下在循环内按 "新到 cache" 累加
    if incremental:
        new_count = len(diff_aweme_ids(
            [v["video_id"] for v in videos], synced_ids
        ))
    else:
        new_count = 0
    logger.info(
        f"[Douyin] _upsert_to_folder: {folder_title}, "
        f"mode={'incremental' if incremental else 'full'}, "
        f"processing {len(videos)} videos"
    )

    # === 批量预加载：解决 N+1 查询问题 ===
    all_vids = [v["video_id"] for v in videos]
    cache_map: dict[str, object] = {}
    fav_map: dict[str, object] = {}
    if all_vids:
        cache_result = await db.execute(
            sa_select(VideoCache).where(
                VideoCache.bvid.in_(all_vids),
                VideoCache.platform == "douyin",
            )
        )
        cache_map = {c.bvid: c for c in cache_result.scalars()}

        fav_result = await db.execute(
            sa_select(FavoriteVideo).where(
                FavoriteVideo.folder_id == folder.id,
                FavoriteVideo.bvid.in_(all_vids),
            )
        )
        fav_map = {f.bvid: f for f in fav_result.scalars()}

    # 循环处理：从 map 查找而非每次查 DB
    for v in videos:
        vid = v["video_id"]
        if vid not in cache_map:
            cache = VideoCache(
                bvid=vid, platform="douyin",
                title=v.get("title", f"Douyin-{vid}"),
                description=v.get("description", ""),
                owner_name=v.get("author", ""),
                duration=v.get("duration", 0),
                pic_url=v.get("cover_url", ""),
                is_processed=False,
            )
            db.add(cache)
            cache_map[vid] = cache
            if not incremental:
                # 全量同步：新到 cache 计入 new_count（保持原有语义）
                new_count += 1

        if vid not in fav_map:
            try:
                db.add(FavoriteVideo(
                    folder_id=folder.id, bvid=vid, is_selected=True
                ))
                await db.flush()
                # 标记已写入，避免 videos 内部重复 video_id 再次 add
                fav_map[vid] = True
            except Exception as ie:
                if "uq_folder_bvid" in str(ie) or "UNIQUE constraint" in str(ie).lower():
                    logger.debug(f"FavoriteVideo 并发写入冲突 [{vid}]，已忽略")
                else:
                    raise

    logger.info(
        f"[Douyin] _upsert_to_folder DONE: {folder_title}, "
        f"new_count={new_count}, total={len(videos)}"
    )
    folder.media_count = len(videos)
    result_dict["synced"] = len(videos)
    result_dict["new"] = new_count
    result_dict["folder_id"] = folder.id
    logger.info(f"[Douyin] {folder_title}: {len(videos)} videos, {new_count} new")


async def _sync_favorites_task(
    task_id: str,
    cookie_str: str,
    douyin_session_id: Optional[str],
    limit: int,
) -> dict:
    """执行抖音同步任务，通过 task_tracker 更新进度与结果，同时返回结果 dict。

    任务结果（sync 结果 dict）写入 ``task_tracker.metadata["result"]``，
    便于调用方通过 task_id 查询任务状态时读取。
    """
    from app.database import get_db_context
    from app.services.platform.douyin import (
        DouyinPlatformService,
        DouyinCookieInvalidError,
    )
    from sqlalchemy import select as sa_select
    from app.models import FavoriteFolder
    from app.services.task_tracker_service import task_tracker

    with TraceContext(step="douyin_sync") as trace_id:
        # 把 trace_id 同步到 task_tracker.metadata，便于跨日志关联
        await task_tracker.update_task(task_id, metadata={"trace_id": trace_id})
        trace_logger.info(
            f"开始抖音同步任务: task_id={task_id} limit={limit} "
            f"session_id={douyin_session_id}"
        )
        try:
            if not douyin_session_id:
                logger.warning(
                    "[Douyin] Sync aborted: no session_id available, "
                    "cannot isolate user data"
                )
                await task_tracker.mark_task_failed(
                    task_id,
                    error_message="会话信息缺失，无法隔离用户数据，请重新登录抖音",
                )
                return {
                    "success": False,
                    "message": "会话信息缺失，无法隔离用户数据，请重新登录抖音",
                }

            user_scope = douyin_session_id
            await task_tracker.start_task(task_id, step="fetching")

            # Check if first-time sync
            first_sync = True
            try:
                async with get_db_context() as _db:
                    existing_folder = await _db.scalar(
                        sa_select(FavoriteFolder).where(
                            FavoriteFolder.platform == "douyin",
                            FavoriteFolder.session_id == f"douyin-{user_scope}-抖音喜欢",
                            FavoriteFolder.title == "抖音喜欢",
                        )
                    )
                    if existing_folder and existing_folder.media_count > 0:
                        first_sync = False
            except Exception:
                pass

            douyin = DouyinPlatformService()
            # 清理 browser_pool 中的过期 context，避免复用已关闭的浏览器实例
            try:
                from app.services.browser_pool import browser_pool
                await browser_pool.cleanup_contexts()
            except Exception as cleanup_err:
                logger.debug(f"清理 browser_pool 过期 context 失败（非致命）: {cleanup_err}")
            try:
                logger.info("[Douyin] Syncing: fetching likes + collects...")
                # 串行执行：fetch_*_via_browser 内部使用 _launch_douyin_context 创建独立浏览器实例，
                # 不复用 browser_pool，避免 context 互相影响。串行执行仍保留以降低资源占用。
                # 浏览器关闭错误（Browser has been closed）会自动重试一次。
                like_videos = []
                collect_videos = []
                browser_error = False

                # Like fetch with retry（最多重试 1 次，仅对浏览器关闭错误重试）
                for attempt in range(2):
                    try:
                        like_videos = await douyin.fetch_favorite_videos_via_browser(cookie_str, max_count=limit)
                        break
                    except DouyinCookieInvalidError:
                        raise
                    except Exception as e:
                        error_msg = str(e)
                        if attempt == 0 and (
                            "Target page, context or browser has been closed" in error_msg
                            or "Browser has been closed" in error_msg
                        ):
                            logger.warning(f"[Douyin] Like fetch 首次失败（浏览器关闭），重试一次: {e}")
                            continue
                        logger.error(f"[Douyin] Like fetch 失败: {e}")
                        like_videos = []
                        browser_error = True
                        break

                # Collect fetch with retry（同上模式）
                for attempt in range(2):
                    try:
                        collect_videos = await douyin.fetch_collected_videos_via_browser(cookie_str, max_count=limit)
                        break
                    except DouyinCookieInvalidError:
                        raise
                    except Exception as e:
                        error_msg = str(e)
                        if attempt == 0 and (
                            "Target page, context or browser has been closed" in error_msg
                            or "Browser has been closed" in error_msg
                        ):
                            logger.warning(f"[Douyin] Collect fetch 首次失败（浏览器关闭），重试一次: {e}")
                            continue
                        logger.error(f"[Douyin] Collect fetch 失败: {e}")
                        collect_videos = []
                        browser_error = True
                        break

                collect_flat_videos = list(collect_videos) if collect_videos else []

                if not like_videos and not collect_videos:
                    if browser_error:
                        # 浏览器异常导致空结果，不是真的"没有视频"
                        result = {
                            "success": False,
                            "message": "浏览器实例异常关闭，无法获取视频。请重试。",
                        }
                        await task_tracker.mark_task_failed(
                            task_id, error_message="浏览器实例异常关闭，无法获取视频"
                        )
                        trace_logger.warning(f"抖音同步失败（浏览器异常）: task_id={task_id}")
                        return result
                    result = {
                        "success": True,
                        "first_sync": first_sync,
                        "like": None,
                        "collect": None,
                        "collect_flat": None,
                        "message": "抖音账号下没有喜欢/收藏视频，或所有视频均为私密",
                    }
                    await task_tracker.update_task(
                        task_id,
                        metadata={"result": result},
                    )
                    await task_tracker.complete_task(task_id, success=True)
                    trace_logger.info(f"抖音同步完成（无视频）: task_id={task_id}")
                    return result

                await task_tracker.update_task(
                    task_id,
                    step="upserting",
                    progress=50,
                    metadata={
                        "like_count": len(like_videos) if like_videos else 0,
                        "collect_count": (
                            len(collect_flat_videos) if collect_flat_videos else 0
                        ),
                    },
                )

                like_result = {"synced": 0, "new": 0, "folder_id": None}
                collect_flat_result = {"synced": 0, "new": 0, "folder_id": None}

                logger.info(
                    f"[Douyin] Sync: like_videos={len(like_videos) if like_videos else 0}, "
                    f"collect_flat_videos="
                    f"{len(collect_flat_videos) if collect_flat_videos else 0}"
                )
                if collect_flat_videos:
                    logger.info(
                        f"[Douyin] First 3 collect_flat IDs: "
                        f"{[v.get('video_id','?') for v in collect_flat_videos[:3]]}"
                    )

                async with get_db_context() as db:
                    from app.services.sync_cursor import (
                        get_cursor as _get_dy_cursor,
                        load_synced_ids as _load_dy_synced_ids,
                        upsert_cursor as _upsert_dy_cursor,
                    )

                    # 增量同步：按 folder_title 维度读取已同步 aweme_id 集合，
                    # 仅追加 diff 出的新增视频；首次同步（无游标）走全量替换。
                    # 游标读取失败时降级为全量同步（synced_ids=None），保证同步主流程不中断。
                    like_synced_ids: Optional[set] = None
                    if like_videos:
                        try:
                            like_cursor = await _get_dy_cursor(db, "douyin", "抖音喜欢")
                            if like_cursor is not None:
                                like_synced_ids = _load_dy_synced_ids(like_cursor)
                        except Exception as cursor_err:
                            logger.warning(f"[Douyin] 读取抖音喜欢游标失败，降级全量同步: {cursor_err}")
                            like_synced_ids = None
                        await _upsert_to_folder(
                            db, like_videos, "抖音喜欢", like_result, user_scope,
                            synced_ids=like_synced_ids,
                        )
                        # 同步完成后更新游标为当前完整 aweme_id 集合
                        try:
                            await _upsert_dy_cursor(
                                db, "douyin", "抖音喜欢",
                                last_synced_ids={v["video_id"] for v in like_videos},
                            )
                        except Exception as cursor_err:
                            logger.warning(f"[Douyin] 更新抖音喜欢游标失败: {cursor_err}")

                    collect_synced_ids: Optional[set] = None
                    if collect_flat_videos:
                        try:
                            collect_cursor = await _get_dy_cursor(db, "douyin", "抖音收藏-视频")
                            if collect_cursor is not None:
                                collect_synced_ids = _load_dy_synced_ids(collect_cursor)
                        except Exception as cursor_err:
                            logger.warning(f"[Douyin] 读取抖音收藏-视频游标失败，降级全量同步: {cursor_err}")
                            collect_synced_ids = None
                        await _upsert_to_folder(
                            db,
                            collect_flat_videos,
                            "抖音收藏-视频",
                            collect_flat_result,
                            user_scope,
                            synced_ids=collect_synced_ids,
                        )
                        try:
                            await _upsert_dy_cursor(
                                db, "douyin", "抖音收藏-视频",
                                last_synced_ids={v["video_id"] for v in collect_flat_videos},
                            )
                        except Exception as cursor_err:
                            logger.warning(f"[Douyin] 更新抖音收藏-视频游标失败: {cursor_err}")

                    await db.commit()

                result = {
                    "success": True,
                    "first_sync": first_sync,
                    "like": like_result if like_videos else None,
                    "collect": None,
                    "collect_flat": (
                        collect_flat_result if collect_flat_videos else None
                    ),
                }
                await task_tracker.update_task(
                    task_id,
                    progress=100,
                    metadata={"result": result},
                )
                await task_tracker.complete_task(task_id, success=True)
                trace_logger.info(f"抖音同步完成: task_id={task_id}")
                return result
            finally:
                await douyin.close()
        except DouyinCookieInvalidError as e:
            # cookie 失效是预期异常，单独处理避免打印完整 traceback 噪音
            logger.warning(f"[Douyin] Cookie invalid: {e}")
            try:
                await task_tracker.mark_task_failed(task_id, error_message=str(e))
            except Exception:
                pass
            return {
                "success": False,
                "message": str(e),
            }
        except Exception as e:
            logger.error(f"[Douyin] Sync task failed: {e}")
            logger.exception("[Douyin] _sync_favorites_task traceback")
            try:
                await task_tracker.mark_task_failed(task_id, error_message=str(e))
            except Exception:
                pass
            return {
                "success": False,
                "message": f"同步失败: {e}",
            }


# ---- Routes ----
@router.get("/qrcode", response_model=QRCodeResponse)
async def gen_qr():
    # 登录入口设置 trace_id，便于关联后续 _safe_poll 后台任务的日志。
    # FastAPI async 端点运行在独立 asyncio task 中，ContextVar 在 task 结束后
    # 自动 reset，不会泄漏到其他请求；_safe_poll 通过 _spawn_background_task
    # 启动时会 copy 当前 context（含 trace_id）。
    set_trace_id(str(uuid.uuid4()))
    trace_logger.info("开始生成抖音登录二维码")
    start_ts = time.monotonic()

    # Langfuse trace：可选，未启用时为 no-op
    from app.services.langfuse_tracer import start_trace, end_trace, set_tag
    start_trace(
        "douyin_qr_login",
        tags={"platform": "douyin", "auth_method": "qrcode"},
    )
    # 优化3：先查缓存（暂禁用，排查扫码无反应问题）
    # cached = _get_cached_qr()
    # if cached and cached.get("b64"):
    #     trace_logger.info("[Douyin] QR cache hit, returning cached")
    #     return QRCodeResponse(session_key=cached["session_key"], qrcode_image_base64=cached["b64"])
    try:
        # Langfuse span: 浏览器页面获取
        from app.services.langfuse_tracer import start_span, end_span
        start_span("acquire_qr_page")
        entry = await acquire_qr_page()
        end_span("success", output={"from_warmup": entry.get("from_warmup", False)})
    except BrowserLaunchError as e:
        # Langfuse: 结束 span 并记录浏览器启动失败
        from app.services.langfuse_tracer import end_trace, end_span
        end_span(
            "error",
            output={
                "error_type": "browser_launch_error",
                "sub_type": e.error_type,
                "detail": e.detail,
                "message": str(e),
            },
        )
        end_trace(
            "error",
            output={
                "error_type": "browser_launch_error",
                "sub_type": e.error_type,
                "detail": e.detail,
                "message": str(e),
            },
        )
        logger.error(f"[Douyin] acquire_qr_page failed: {e.error_type}: {e}")
        raise HTTPException(503, detail=str(e)) from e
    except Exception as e:
        # Langfuse: 结束 span 并记录未知错误
        from app.services.langfuse_tracer import end_trace, end_span
        end_span(
            "error",
            output={
                "error_type": "unknown",
                "exception_type": type(e).__name__,
                "message": str(e),
            },
        )
        end_trace(
            "error",
            output={
                "error_type": "unknown",
                "exception_type": type(e).__name__,
                "message": str(e),
            },
        )
        logger.error(f"[Douyin] acquire_qr_page failed: {type(e).__name__}: {e}")
        raise HTTPException(503, detail=f"浏览器启动失败：{type(e).__name__}，请稍后重试") from e
    ctx = entry["ctx"]
    page = entry["page"]
    captured = entry["captured"]
    on_response = entry["on_response"]
    qr_event = entry["qr_event"]
    qrconnect_status = entry.get("qrconnect_status", {"status": None, "redirect_url": None, "updated": False})
    from_warmup = entry.get("from_warmup", False)
    key = str(uuid.uuid4())
    qr_b64 = None
    sso_token = None
    try:
        # 优化4：通过 modal_id=login URL 直接获取 QR 码
        # 关键发现：访问 https://www.douyin.com/?modal_id=login 页面会自动弹出登录弹窗并加载 QR 码，
        # 完全不需要点击登录按钮，绕开了 CDP 被重 JS 阻塞的问题。
        # 耗时从 120-150s 降到 10-15s。
        # 抖音QR码有效期约60秒，超过后扫描会跳转到默认页面而非登录确认
        _QR_MAX_AGE_SEC = 50  # 安全阈值，比抖音实际有效期略短

        # 路径1：预热页已捕获到（最快）—— 检查是否过期
        if captured.get("b64"):
            captured_at = captured.get("captured_at")
            age = time.monotonic() - captured_at if captured_at else 999
            if age < _QR_MAX_AGE_SEC:
                qr_b64 = captured["b64"]
                sso_token = captured.get("token")
                logger.info(f"[Douyin] QR captured from prewarmed page (instant, age={age:.0f}s)")
            else:
                # QR码已过期，重新导航到 modal_id=login URL 刷新
                logger.info(f"[Douyin] Prewarmed QR expired (age={age:.0f}s), refreshing via re-nav...")
                captured["b64"] = None
                captured["token"] = None
                captured["captured_at"] = None
                qr_event.clear()
                # 重新导航即可刷新 QR 码（页面会自动重新发起 get_qrcode 请求）
                try:
                    await page.goto("https://www.douyin.com/?modal_id=login", wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    logger.debug(f"[Douyin] refresh goto tolerated: {e}")

        if not qr_b64:
            # 页面还没有 QR 码（冷启动或刷新中），等待网络拦截捕获
            # 新方案：直接访问 modal_id=login URL，页面自动弹出登录弹窗并加载 QR 码
            # 不需要点击登录按钮，绕开了 CDP 被重 JS 阻塞的问题
            logger.info("[Douyin] Waiting for QR code via network intercept (modal_id=login)...")

            # 主路径：轮询等待网络拦截捕获 QR 码
            # 通常 10-15 秒就能捕获到，最多等 60 秒
            poll_deadline = time.monotonic() + 60
            while time.monotonic() < poll_deadline:
                if captured.get("b64"):
                    qr_b64 = captured["b64"]
                    sso_token = captured.get("token")
                    logger.info("[Douyin] QR captured via network intercept (modal_id=login)")
                    break
                await asyncio.sleep(1)
            else:
                logger.warning("[Douyin] QR polling timed out (60s) without capture")

        if not qr_b64:
            logger.warning("[Douyin] All QR capture methods failed")
            # 失败时截取页面截图用于诊断
            shot_b64 = await _safe_page_screenshot(page)
            elapsed = time.monotonic() - start_ts
            if ctx:
                await ctx.close()
            detail_parts = [
                f"阶段: wait_qr_capture",
                f"耗时: {elapsed:.1f}s",
                f"来源: {'预热池' if from_warmup else '即时创建'}",
            ]
            # 尝试从页面获取更多诊断信息
            try:
                url = page.url if not page.is_closed() else "unknown"
                detail_parts.append(f"页面URL: {url[:80]}")
            except Exception:
                pass
            raise QRCaptureError(
                "二维码获取失败，抖音可能要求滑块验证或网络异常。请使用 Cookie 登录方式。",
                stage="wait_qr_capture",
                elapsed_sec=elapsed,
                detail="; ".join(detail_parts),
                screenshot_b64=shot_b64,
            )

        # 校验QR码图像有效性（格式+尺寸+大小），防止返回空白透明图导致前端显示异常
        if not _validate_qr_image(qr_b64):
            logger.warning("[Douyin] QR image validation failed (invalid format/size), discarding")
            shot_b64 = await _safe_page_screenshot(page)
            elapsed = time.monotonic() - start_ts
            if ctx:
                await ctx.close()
            raise QRCaptureError(
                "二维码数据异常，请刷新重试",
                stage="validate_qr",
                elapsed_sec=elapsed,
                detail="QR 图像验证失败（格式/尺寸不合法）",
                screenshot_b64=shot_b64,
            )

        # 注意：不移除 response 监听器
        # 监听器同时监听 get_qrcode 和 check_qrconnect，
        # 轮询阶段需要继续监听 check_qrconnect 来获取扫码状态。
        # 移除 _extract_token 调用：page.evaluate 会阻塞 CDP 通道，
        # 可能导致 check_qrconnect 响应事件无法及时传递。
        # sso_token 已从网络拦截中获取，不需要额外提取。
        if not sso_token:
            sso_token = captured.get("token")
        # Capture baseline cookies for cookie-delta monitoring（加超时防止 page.context.cookies 阻塞）
        baseline_cookies = {}
        try:
            for c in await asyncio.wait_for(page.context.cookies(), timeout=10):
                baseline_cookies[c["name"]] = c["value"]
        except asyncio.TimeoutError:
            logger.warning("[Douyin] baseline cookies capture timed out (10s), cookie delta may be less accurate")
        except Exception:
            pass

        # 关键修复：注入 JS 轮询器，不依赖页面 SDK 是否正常轮询
        # 预热页面刷新后原有监听器可能失效，注入的 JS 轮询器在页面上下文中
        # 自主调用 check_qrconnect，和抖音 SDK 完全一致，不会被风控拦截
        await _inject_qr_poller(page, sso_token)

        _login_sessions[key] = {
            "token": sso_token, "page": page, "context": ctx,
            "status": "waiting", "message": "等待扫码",
            "baseline_cookies": baseline_cookies,
            "sso_token": sso_token,
            "qrconnect_status": qrconnect_status,
            "created_at": time.monotonic(),
            "from_warmup": from_warmup,
        }
        logger.info(f"[Douyin] Baseline cookies captured: {list(baseline_cookies.keys())}")
        cancel_event = asyncio.Event()
        _login_sessions[key]["_cancel_event"] = cancel_event

        # 优化3：写入缓存
        _set_qr_cache(qr_b64, sso_token, key)

        async def _safe_poll():
            try:
                await _poll(key, page, cancel_event)
            except Exception as e:
                logger.error(f"[Douyin] Poll task crashed for {key[:8]}: {e}")
                logger.exception("[Douyin] poll task traceback")
                if key in _login_sessions:
                    _login_sessions[key]["status"] = "error"
                    _login_sessions[key]["message"] = "登录服务异常"
            finally:
                # 轮询结束后（登录成功/过期/异常），自动关闭浏览器窗口
                st = _login_sessions.get(key, {})
                final_status = st.get("status", "unknown")
                if final_status == "confirmed":
                    # 登录成功：延迟5s关闭浏览器，让前端有时间获取登录结果
                    logger.info(f"[Douyin] Login confirmed, closing browser in 5s")
                    _spawn_background_task(_delayed_cleanup(key, 5))
                else:
                    # 非成功状态：30s后清理（保留时间让前端获取错误信息）
                    _spawn_background_task(_delayed_cleanup(key, 30))
        _spawn_background_task(_safe_poll())
        # Langfuse: 记录 trace 成功输出
        from app.services.langfuse_tracer import end_trace
        end_trace(
            "success",
            output={
                "session_key": key[:8] + "..." if len(key) > 8 else key,
                "from_warmup": from_warmup,
                "qr_image_size": len(qr_b64),
            },
        )
        # 等待 3 秒确保抖音页面完全加载，再返回二维码给前端
        # 页面未完全加载就返回二维码，用户扫码后 SDK 可能无法正常轮询，
        # 导致扫码状态停留在 "scanned" 而不触发后续登录流程
        logger.info("[Douyin] Waiting 3s for page to fully load before returning QR to frontend")
        await asyncio.sleep(3)
        return QRCodeResponse(session_key=key, qrcode_image_base64=qr_b64)
    except QRCaptureError as e:
        # Langfuse: 结束 span 并记录 QR 捕获失败
        from app.services.langfuse_tracer import end_trace, end_span
        end_span(
            "error",
            output={
                "error_type": "qr_capture_error",
                "stage": e.stage,
                "detail": e.detail,
                "message": str(e),
            },
        )
        end_trace(
            "error",
            output={
                "error_type": "qr_capture_error",
                "stage": e.stage,
                "detail": e.detail,
                "message": str(e),
            },
        )
        # 携带诊断信息的二维码获取失败
        logger.error(f"[Douyin] QR capture failed at stage={e.stage}: {e}")
        detail = e.detail
        if e.screenshot_b64:
            detail += f" | 截图大小: {len(e.screenshot_b64)} chars (base64)"
        raise HTTPException(
            503,
            detail=f"{e} [诊断: {detail}]",
        ) from e
    except HTTPException:
        if ctx:
            await ctx.close()
        raise
    except Exception as e:
        if ctx:
            await ctx.close()
        # Langfuse: 记录未知的 QR 生成错误
        from app.services.langfuse_tracer import end_trace
        end_trace(
            "error",
            output={
                "error_type": "qr_generation_error",
                "exception_type": type(e).__name__,
                "message": str(e),
            },
        )
        logger.error(f"[Douyin] QR generation error: {e}")
        logger.exception("[Douyin] QR generation traceback")
        raise HTTPException(503, detail=f"二维码获取失败：{type(e).__name__}，请稍后重试") from e

@router.get("/qrcode/poll", response_model=QRCodePollResponse)
async def poll_qr(session_key: str = Query(...)):
    with TraceContext(step="douyin_qrcode_poll"):
        st = _login_sessions.get(session_key)
        if not st:
            return QRCodePollResponse(status="expired", message="会话已过期")
        status = st.get("status", "waiting")
        msg = st.get("message", "")
        if status == "confirmed":
            cs = st.get("cookie_str", "")
            ui = st.get("user_info", {})
            uid = ui.get("uid", "")
            nn = ui.get("nickname", "抖音用户")
            sid = str(uuid.uuid4())
            try:
                from app.database import get_db_context
                from sqlalchemy import update as sa_update
                async with get_db_context() as db:
                    # 失效旧的抖音有效会话，避免数据库中存在多个 is_valid=True 的记录
                    await db.execute(
                        sa_update(UserSessionModel)
                        .where(UserSessionModel.platform == Platform.DOUYIN)
                        .where(UserSessionModel.is_valid == True)  # noqa: E712
                        .values(is_valid=False)
                    )
                    dbs = UserSessionModel(
                        session_id=sid, platform=Platform.DOUYIN,
                        douyin_cookie=encrypt_secret(cs), douyin_uid=uid,
                        bili_uname=nn, username=nn, user_id=uid,
                        is_valid=True
                    )
                    db.add(dbs)
                    await db.commit()
            except Exception as e:
                # DB 持久化失败时不能返回 confirmed，否则服务重启后 session_id 失效
                logger.error(f"[Douyin] DB save error: {e}")
                await _cleanup(session_key)
                return QRCodePollResponse(
                    status="error",
                    message="登录会话持久化失败，请重试",
                )
            from app.routers.auth import login_sessions
            try:
                login_sessions["douyin-active"] = {
                    "session_id": sid, "douyin_cookie": cs,
                    "user_info": {"uid": uid, "nickname": nn},
                    "platform": Platform.DOUYIN,
                }
            except Exception as e:
                logger.error(f"[Douyin] login_sessions write error: {e}")
                await _cleanup(session_key)
                return QRCodePollResponse(
                    status="error",
                    message="登录会话内存写入失败，请重试",
                )
            try:
                await _cleanup(session_key)
            except Exception as e:
                logger.warning(f"[Douyin] _cleanup error in confirmed path: {e}")
            trace_logger.info(f"扫码登录确认成功: sid={sid} uid={uid}")
            return QRCodePollResponse(
                status="confirmed", message="登录成功",
                session_id=sid,
                user_info={"uid": uid, "nickname": nn, "avatar": ui.get("avatar", "")}
            )
        if status in ("expired", "error"):
            await _cleanup(session_key)
        return QRCodePollResponse(status=status, message=msg)

@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(db: AsyncSession = Depends(get_db)):
    from app.routers.auth import login_sessions
    c = login_sessions.get("douyin-active")
    if c and c.get("douyin_cookie"):
        return AuthStatusResponse(
            logged_in=True,
            uid=c.get("user_info",{}).get("uid",""),
            nickname=c.get("user_info",{}).get("nickname","")
        )
    r = await db.execute(
        select(UserSessionModel)
        .where(UserSessionModel.douyin_cookie.isnot(None))
        .where(UserSessionModel.platform == Platform.DOUYIN)
        .where(UserSessionModel.is_valid == True)
        .order_by(UserSessionModel.created_at.desc())
        .limit(1)
    )
    s = r.scalar_one_or_none()
    if s and s.douyin_cookie:
        return AuthStatusResponse(logged_in=True, uid=s.douyin_uid or "", nickname=s.bili_uname or "")
    return AuthStatusResponse(logged_in=False)

@router.delete("/logout", response_model=LogoutResponse)
async def logout(
    session_id: Optional[str] = Query(None, description="可选会话ID，仅登出该会话"),
    db: AsyncSession = Depends(get_db),
):
    """退出抖音登录。

    B8: 默认仅登出当前 douyin-active 会话；若传入 session_id 则只置该 session 无效，
    避免清空所有用户的抖音登录态。
    """
    from app.routers.auth import login_sessions
    active = login_sessions.pop("douyin-active", None)
    active_sid = active.get("session_id") if active else None

    target_sids: set[str] = set()
    if session_id:
        target_sids.add(session_id)
    if active_sid:
        target_sids.add(active_sid)

    if target_sids:
        rows = await db.execute(
            select(UserSessionModel).where(
                UserSessionModel.session_id.in_(target_sids),
                UserSessionModel.platform == Platform.DOUYIN,
            )
        )
        for s in rows.scalars().all():
            s.is_valid = False
    await db.commit()
    return LogoutResponse(message="已退出抖音登录")

@router.post("/login", response_model=CookieLoginResponse)
async def cookie_login(payload: CookieLoginRequest, db: AsyncSession = Depends(get_db)):
    with TraceContext(step="douyin_cookie_login"):
        if not payload.cookie or not payload.cookie.strip():
            return CookieLoginResponse(success=False, message="Cookie不能为空")
        raw_cookie = payload.cookie.strip()

        # 验证 cookie 有效性并提取用户信息
        user_info: dict = {}
        try:
            user_info = await asyncio.wait_for(
                _fetch_user_info_via_api(raw_cookie), timeout=10
            )
            if not user_info.get("nickname"):
                logger.warning("[Douyin] Cookie 登录：API 未返回用户信息，cookie 可能无效")
                return CookieLoginResponse(
                    success=False,
                    message="Cookie 验证失败：抖音 API 未返回用户信息，请检查 cookie 是否有效",
                )
            logger.info(f"[Douyin] Cookie 登录验证成功: {user_info.get('nickname','?')}")
        except asyncio.TimeoutError:
            logger.warning("[Douyin] Cookie 登录：API 验证超时（10s），允许继续但不保证 cookie 有效")
        except Exception as e:
            logger.warning(f"[Douyin] Cookie 登录：API 验证异常: {e}，允许继续")
            # 不阻断流程，用户可能只是网络问题，cookie 本身可能有效

        uid = user_info.get("uid", "")
        nn = user_info.get("nickname", "抖音用户")

        sid = str(uuid.uuid4())
        # 失效旧的抖音有效会话
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(UserSessionModel)
            .where(UserSessionModel.platform == Platform.DOUYIN)
            .where(UserSessionModel.is_valid == True)  # noqa: E712
            .values(is_valid=False)
        )
        dbs = UserSessionModel(
            session_id=sid,
            platform=Platform.DOUYIN,
            douyin_cookie=encrypt_secret(raw_cookie),
            douyin_uid=uid,
            bili_uname=nn, username=nn, user_id=uid,
            is_valid=True,
        )
        db.add(dbs)
        await db.commit()
        from app.routers.auth import login_sessions
        login_sessions["douyin-active"] = {
            "session_id": sid, "douyin_cookie": raw_cookie,
            "user_info": {"uid": uid, "nickname": nn},
        }
        trace_logger.info(f"Cookie 登录成功: sid={sid}")
        return CookieLoginResponse(success=True, message="Cookie已保存")

@router.post("/sync")
async def sync_favorites(
    limit: int = Query(500, ge=1, le=5000, description="获取上限"),
):
    """同步抖音收藏夹（同步执行，立即返回结果）。

    同时创建 task_tracker 任务用于进度追踪。
    """
    from app.services.task_tracker_service import task_tracker

    with TraceContext(step="douyin_sync"):
        cookie_str, douyin_session_id = await _resolve_douyin_session()

        if not cookie_str:
            return {
                "success": False,
                "message": "请先登录抖音",
            }

        if not douyin_session_id:
            return {
                "success": False,
                "message": "会话信息缺失，请重新登录抖音",
            }

        # 创建任务用于追踪
        task = await task_tracker.create_task(
            video_id=f"douyin_sync:{douyin_session_id}",
            platform="douyin",
            session_id=douyin_session_id,
            task_type="douyin_sync",
            metadata={"limit": limit},
        )

        try:
            # 同步执行
            result = await _sync_favorites_task(task.task_id, cookie_str, douyin_session_id, limit)
            return result
        except Exception as e:
            logger.error(f"抖音同步失败: {e}")
            return {
                "success": False,
                "message": f"同步失败: {e}",
            }

