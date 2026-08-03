"""
ClipMind Platform Abstraction Layer

Douyin (TikTok China) platform service -- Phase 1: manual share-link input.
Uses Playwright to bypass Douyin's anti-bot verification.
"""
import asyncio
import os
import re
import shutil
import tempfile
from typing import Optional, Callable

import httpx
import aiofiles
from loguru import logger

from app.models import VideoContent, ContentSource
from app.services.platform.base import BasePlatformService
from app.services.platform.xbogus import XBogus
from app.services.cancellation import ensure_not_cancelled

RE_VIDEO = re.compile(r"/video/(\d{15,20})")
RE_NOTE = re.compile(r"/note/(\d{15,20})")
RE_AWEME = re.compile(r"aweme_id['\"]?\s*[:=]\s*['\"]?(\d{15,20})")


class DouyinCookieInvalidError(RuntimeError):
    """抖音 cookie 在服务端被判定为未登录态。

    触发场景：
    - cookie 已过期或被服务端主动失效
    - 服务端 IP 校验失败（cookie 在境外 IP 下被拒绝识别）
    - cookie 缺少关键字段（如 msToken / sessionid）

    抛出此异常时调用方应直接返回明确错误，而不是默默返回空列表，
    否则用户会以为"没有喜欢/收藏视频"而非"登录态失效"。
    """


# Standard Douyin API query params (matches douyin-downloader _default_query)
DOUYIN_API_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "pc_libra_divert": "Windows",
    "version_code": "290100",
    "version_name": "29.1.0",
    "cookie_enabled": "true",
    "screen_width": "1536",
    "screen_height": "864",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "139.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "139.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "16",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "200",
}


# Collect API params (lighter set, matches douyin-downloader _build_collect_page_params)
# Collect API params: full _default_query set with version overrides for collect endpoints
DOUYIN_COLLECT_API_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "pc_libra_divert": "Windows",
    "version_code": "170400",
    "version_name": "17.4.0",
    "cookie_enabled": "true",
    "screen_width": "1536",
    "screen_height": "864",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "139.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "139.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "16",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "200",
}


class DouyinPlatformService(BasePlatformService):
    """Douyin platform service using Playwright for data extraction."""

    platform = "douyin"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    def __init__(
        self,
        use_browser_pool: bool = True,
        qrcode_timeout: int = 10,
        qrcode_retries: int = 2,
    ):
        self.client = httpx.AsyncClient(
            timeout=30.0, headers=self.HEADERS, follow_redirects=True, trust_env=True
        )
        self._browser = None
        self._playwright = None
        self._browser_lock = asyncio.Lock()
        self.use_browser_pool = use_browser_pool
        self.qrcode_timeout = qrcode_timeout
        self.qrcode_retries = qrcode_retries

    # ------------------------------------------------------------------
    #  Browser helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _aweme_to_dict(item: dict) -> dict:
        """统一抖音 aweme 对象转 VideoInfo dict"""
        video = item.get("video") or {}
        author = item.get("author") or {}
        cover_list = (video.get("cover") or {}).get("url_list") or []
        aid = str(item.get("aweme_id", ""))
        return {
            "video_id": aid,
            "title": item.get("desc") or (f"Douyin-{aid}"),
            "description": item.get("desc", ""),
            "author": author.get("nickname", ""),
            "duration": video.get("duration", 0),
            "cover_url": cover_list[0] if cover_list else "",
        }

    @staticmethod
    def _parse_cookie_str(cookie_str: str) -> list[dict]:
        """将 cookie 字符串解析为 Playwright cookie 对象列表"""
        cookie_list = []
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, _, value = item.partition("=")
                cookie_list.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".douyin.com",
                    "path": "/",
                })
        return cookie_list

    def _log_launch_diagnostic(self, browser) -> None:
        """输出 browser.new_context/new_page 失败时的环境诊断日志。

        在 _launch_douyin_context 的 new_context/new_page 异常路径调用，
        包含 Chromium 路径、/dev/shm 大小、DISPLAY、Playwright 版本、browser 连接状态，
        便于排查 "Target page, context or browser has been closed" 等环境问题。
        每个诊断项均独立 try/except，单项失败不影响其余项输出。
        """
        chromium_path = "unknown"
        try:
            from playwright._impl._driver import compute_driver_executable
            chromium_path = compute_driver_executable()
        except Exception:
            chromium_path = "unknown"

        shm_size_mb = "unknown"
        try:
            stat = os.statvfs("/dev/shm")
            shm_size_mb = stat.f_frsize * stat.f_blocks // (1024 * 1024)
        except Exception:
            shm_size_mb = "unknown"

        display = os.environ.get("DISPLAY", "not set")

        pw_version = "unknown"
        try:
            import playwright
            pw_version = playwright.__version__
        except Exception:
            pw_version = "unknown"

        is_connected = "unknown"
        try:
            is_connected = browser.is_connected()
        except Exception:
            is_connected = "unknown"

        logger.error(
            f"[Douyin] 诊断信息:\n"
            f"  Chromium 路径: {chromium_path}\n"
            f"  /dev/shm 大小: {shm_size_mb} MB\n"
            f"  DISPLAY: {display}\n"
            f"  Playwright 版本: {pw_version}\n"
            f"  browser.is_connected: {is_connected}"
        )

    async def _launch_douyin_context(self, cookie_str: str):
        """启动 Playwright context，返回 (playwright, browser, context, page)

        每次创建独立的 playwright + browser 实例，避免多个 fetch 之间
        因共享 browser_pool 导致 context 互相影响（一个 context.close()
        会使另一个 fetch 的 new_context() 失败）。

        异常路径下按创建反序清理资源，避免半启动状态残留。
        """
        cookie_list = self._parse_cookie_str(cookie_str)
        context = None
        playwright = None
        browser = None
        try:
            from playwright.async_api import async_playwright
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                context = await browser.new_context(
                    user_agent=self.HEADERS["User-Agent"],
                    viewport={"width": 1920, "height": 1080},
                    locale="zh-CN",
                )

                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => false });"
                    "window.chrome = { runtime: {} };"
                )
                await context.add_cookies(cookie_list)
                page = await context.new_page()
            except Exception as e:
                # new_context / new_page 失败时输出诊断信息，便于排查环境问题
                logger.error(f"[Douyin] browser.new_context 失败: {type(e).__name__}: {e}")
                self._log_launch_diagnostic(browser)
                # 尝试释放 browser 资源，失败时仅记录 debug 日志
                try:
                    await browser.close()
                except Exception as close_err:
                    logger.debug(f"[Douyin] browser.close 清理失败: {close_err}")
                browser = None
                raise

            return playwright, browser, context, page
        except Exception:
            if context is not None:
                try:
                    await context.close()
                except Exception as close_err:
                    logger.debug(f"Douyin context cleanup error: {close_err}")
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass
            raise

    async def _check_cookie_login_state(self, page) -> None:
        """预检 cookie 在抖音服务端是否被识别为登录态。

        通过浏览器内 fetch 调用 user/profile/self API（带完整 cookie + Origin），
        若服务端返回 status_code=8（未登录）则抛出 DouyinCookieInvalidError。

        必须在 page 已加载 douyin.com 域名页面后调用（否则 fetch 会因跨域被拦截）。
        通常在 _launch_douyin_context 后调用 page.goto('https://www.douyin.com/')
        再调用本方法。

        设计动机：抖音服务端基于 IP+设备指纹校验 sessionid，云端 IP 会被拒绝识别。
        若不预检，后续 fetch_favorite_videos_via_browser 会拿到空 aweme_list，
        sync_favorites 会返回 success=true 但数据为空，用户误以为"没有喜欢视频"
        而非"登录态失效"。预检失败时直接抛异常，让上层返回明确错误。
        """
        result = await page.evaluate("""async () => {
            try {
                const r = await fetch('https://www.douyin.com/aweme/v1/web/user/profile/self/?device_platform=webapp&aid=6383', {
                    credentials: 'include',
                    headers: { 'Accept': 'application/json' },
                });
                const data = await r.json();
                return {
                    status_code: data.status_code,
                    status_msg: data.status_msg || '',
                    has_user: !!(data.user),
                };
            } catch (e) {
                return { error: e.message || String(e) };
            }
        }""")
        if not result or result.get("error"):
            raise DouyinCookieInvalidError(
                f"无法校验抖音登录态：{result.get('error') if result else 'page evaluate 返回 None'}"
            )
        sc = result.get("status_code")
        if sc == 8 or (sc not in (0, None) and not result.get("has_user")):
            msg = result.get("status_msg") or "用户未登录"
            raise DouyinCookieInvalidError(
                f"抖音 cookie 在服务端被判定为未登录（status_code={sc}, msg={msg}）。"
                "可能原因：1) cookie 已过期；2) 服务端 IP 校验失败（cookie 在境外 IP 下被拒绝识别）；"
                "3) 用户已退出登录。请在本地环境（中国大陆 IP）重新获取 cookie。"
            )
        logger.info(f"[Douyin] cookie login state OK (status_code={sc})")

    async def _get_browser(self):
        """Lazy-init a headless Chromium browser via Playwright.

        当 use_browser_pool=True 时，从全局 BrowserPool 获取共享浏览器实例；
        否则使用实例级独立浏览器（原有逻辑）。
        """
        if self.use_browser_pool:
            from app.services.browser_pool import browser_pool
            await browser_pool.initialize()
            return browser_pool._browser

        if self._browser is not None:
            return self._browser

        async with self._browser_lock:
            if self._browser is not None:
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise RuntimeError("playwright not installed – run: pip install playwright && playwright install chromium")

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            return self._browser

    async def _scrape_video_page(self, video_id: str) -> Optional[dict]:
        """Open douyin.com/video/{id} in headless browser and intercept the aweme/detail API response."""
        browser = await self._get_browser()
        context = await browser.new_context(
            user_agent=self.HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()

        api_data: Optional[dict] = None

        async def _on_response(response):
            nonlocal api_data
            if "aweme/v1/web/aweme/detail" in response.url and api_data is None:
                try:
                    api_data = await response.json()
                except Exception as e:
                    logger.debug(f"Douyin response parse error: {e}")

        page.on("response", _on_response)

        try:
            try:
                await page.goto(
                    f"https://www.douyin.com/video/{video_id}",
                    wait_until="networkidle",
                    timeout=30000,
                )
            except Exception as e:
                logger.warning(f"Douyin page navigation warning [{video_id}]: {e}")

            # Give extra time for async API calls
            await asyncio.sleep(5)
        finally:
            # 无论导航/sleep 是否抛错（含 asyncio.CancelledError）都要关闭 context，
            # 否则会泄露 BrowserContext 进程
            try:
                await context.close()
            except Exception as close_err:
                logger.debug(f"Douyin context close error [{video_id}]: {close_err}")
        return api_data

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    async def parse_share_url(self, share_url: str) -> Optional[dict]:
        """Resolve a v.douyin.com share link -> video metadata."""
        video_id = await self._resolve_video_id(share_url)
        if not video_id:
            logger.warning(f"无法解析链接: {share_url}")
            return None

        info = await self._scrape_video_page(video_id)
        if not info:
            return await self._fallback_parse(video_id)

        detail = (info.get("aweme_detail") or {})
        if not detail:
            return await self._fallback_parse(video_id)

        author = detail.get("author") or {}
        video = detail.get("video") or {}
        cover_list = (video.get("cover") or {}).get("url_list") or []

        return {
            "video_id": video_id,
            "title": detail.get("desc") or f"Douyin-{video_id}",
            "description": detail.get("desc") or "",
            "author": author.get("nickname") or "",
            "cover_url": cover_list[0] if cover_list else "",
            "duration": video.get("duration") or 0,
        }

    async def _fallback_parse(self, video_id: str) -> dict:
        return {
            "video_id": video_id,
            "title": f"Douyin-{video_id}",
            "description": "",
            "author": "",
            "cover_url": "",
            "duration": 0,
        }

    async def _resolve_video_id(self, share_url: str) -> Optional[str]:
        """Follow v.douyin.com redirect to get the numeric video ID."""
        try:
            resp = await self.client.get(share_url)
        except Exception as e:
            logger.warning(f"Share-link request failed: {e}")
            return self._extract_video_id_from_url(share_url)

        # Try redirect URL first
        final_url = str(resp.url) if resp.url else ""
        vid = self._extract_video_id_from_url(final_url)
        if vid:
            return vid

        # Try parsing the response body
        body = resp.text or ""
        for pattern in (RE_VIDEO, RE_NOTE, RE_AWEME):
            m = pattern.search(body)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _extract_video_id_from_url(url: str) -> Optional[str]:
        for pattern in (RE_VIDEO, RE_NOTE):
            m = pattern.search(url)
            if m:
                return m.group(1)
        # Try query params
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        modal_id = params.get("modal_id", [None])[0]
        if modal_id and modal_id.isdigit() and 15 <= len(modal_id) <= 20:
            return modal_id
        return None

    async def get_video_info(self, video_id: str) -> Optional[dict]:
        """Get Douyin video metadata via browser scraping."""
        info = await self._scrape_video_page(video_id)
        if not info:
            return None
        detail = (info.get("aweme_detail") or {})
        if not detail:
            return None
        author = detail.get("author") or {}
        video = detail.get("video") or {}
        cover_list = (video.get("cover") or {}).get("url_list") or []
        return {
            "bvid": video_id,
            "title": detail.get("desc") or f"Douyin-{video_id}",
            "description": detail.get("desc") or "",
            "owner_name": author.get("nickname") or "",
            "owner_mid": author.get("uid") or 0,
            "duration": video.get("duration") or 0,
            "pic_url": cover_list[0] if cover_list else "",
            "cid": None,
        }

    # ------------------------------------------------------------------
    #  Content fetching (ASR pipeline)
    # ------------------------------------------------------------------

    async def fetch_content(
        self,
        video_id,
        asr_service,
        cancel_check=None,
        progress_callback: Optional[Callable[[str, str, str], None]] = None,
    ) -> VideoContent:
        """Fetch Douyin video content using browser extraction + shared ASR pipeline.

        Args:
            progress_callback: 可选 (step, status, message) -> None 回调。
                step 取值：scrape_page / download_video / extract_audio / asr / done。
        """
        def _emit(step: str, status: str, message: str) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(step, status, message)
                except Exception as cb_err:
                    logger.debug(f"progress_callback 异常被忽略: {cb_err}")

        ensure_not_cancelled(cancel_check)

        _emit("scrape_page", "running", "正在抓取抖音视频页面...")
        # Scrape video data via Playwright
        api_data = await self._scrape_video_page(video_id)
        detail = (api_data or {}).get("aweme_detail") or {}

        title = detail.get("desc") or f"Douyin-{video_id}"
        desc = detail.get("desc") or ""
        author_info = detail.get("author") or {}
        owner = author_info.get("nickname") or ""

        # Get download URL from intercepted data
        download_url = self._extract_download_url(detail)

        asr_text = None
        if download_url:
            _emit("download_video", "running", "正在下载抖音视频...")
            asr_text = await self._download_and_transcribe(
                video_id,
                download_url,
                asr_service,
                cancel_check,
                progress_callback=_emit,
            )

        ensure_not_cancelled(cancel_check)

        if asr_text:
            _emit("done", "completed", "转写完成")
            return VideoContent(
                bvid=video_id,
                title=title,
                content=asr_text,
                source=ContentSource.ASR,
                description=desc,
                owner_name=owner,
                duration=detail.get("video", {}).get("duration"),
                platform="douyin",
            )
        _emit("done", "completed", "使用基础信息兜底")
        # ASR 失败兜底：拼上简介，避免标题过短导致内容不足 10 字符无法入库
        basic_content = f"视频标题：{title}"
        if desc:
            basic_content += f"\n\n视频简介：{desc}"
        return VideoContent(
            bvid=video_id,
            title=title,
            content=basic_content,
            source=ContentSource.BASIC_INFO,
            description=desc,
            owner_name=owner,
            duration=detail.get("video", {}).get("duration"),
            platform="douyin",
        )

    @staticmethod
    def _extract_download_url(detail: dict) -> Optional[str]:
        """Extract a downloadable video URL from aweme detail."""
        video = detail.get("video") or {}
        # Try download_addr first (no watermark), then play_addr
        for key in ("download_addr", "play_addr"):
            addr = video.get(key) or {}
            url_list = addr.get("url_list") or []
            if url_list:
                return url_list[0]
        return None

    async def _download_and_transcribe(
        self,
        video_id: str,
        download_url: str,
        asr_service,
        cancel_check=None,
        progress_callback: Optional[Callable[[str, str, str], None]] = None,
    ) -> Optional[str]:
        """Download video, extract audio, run ASR."""
        def _emit(step: str, status: str, message: str) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(step, status, message)
                except Exception as cb_err:
                    logger.debug(f"progress_callback 异常被忽略: {cb_err}")

        ensure_not_cancelled(cancel_check)
        tmp_dir = None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="dy_asr_")
            video_path = os.path.join(tmp_dir, f"{video_id}.mp4")
            audio_path = os.path.join(tmp_dir, f"{video_id}.wav")

            if not await self._download_video(download_url, video_path):
                return None

            ensure_not_cancelled(cancel_check)
            _emit("extract_audio", "running", "正在提取音频...")

            if not await self._extract_audio(video_path, audio_path):
                return None

            ensure_not_cancelled(cancel_check)
            _emit("asr", "running", "正在转写音频...")

            return await asr_service.transcribe_local_file(audio_path)
        except Exception as e:
            logger.warning(f"ASR pipeline error [{video_id}]: {e}")
            return None
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _download_video(self, url: str, file_path: str) -> bool:
        """Download video file to disk (async, non-blocking IO)."""
        try:
            headers = {
                **self.HEADERS,
                "Referer": "https://www.douyin.com/",
            }
            async with self.client.stream("GET", url, headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    logger.warning(f"Download failed: status={resp.status_code}")
                    return False
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            await f.write(chunk)
            return os.path.getsize(file_path) > 0
        except Exception as e:
            logger.warning(f"Download error: {e}")
            return False

    async def download_audio_to_file(self, audio_url: str, file_path: str) -> bool:
        """Download video/audio stream to a local file path."""
        return await self._download_video(audio_url, file_path)

    @staticmethod
    async def _extract_audio(video_path: str, audio_path: str) -> bool:
        """Use ffmpeg to extract 16kHz mono WAV (async, non-blocking)."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not found")
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-y", "-i", video_path,
                "-ac", "1", "-ar", "16000", "-vn", audio_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    # 等待子进程真正退出，避免僵尸进程；再加 5s 超时防止 kill 后仍卡住
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception as e:
                    logger.debug(f"ffmpeg kill/wait error: {e}")
                logger.warning("ffmpeg timed out after 120s")
                return False
            if proc.returncode != 0:
                err = (stderr or b"").decode("utf-8", errors="ignore")
                logger.warning(f"ffmpeg error: {err[:200]}")
                return False
            return os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
        except Exception as e:
            logger.warning(f"ffmpeg exception: {e}")
            return False

    # ------------------------------------------------------------------
    #  Cookie-based API (for authenticated operations)
    # ------------------------------------------------------------------

    async def get_user_info_by_cookie(self, cookies: dict) -> Optional[dict]:
        """Validate cookies by fetching Douyin user info."""
        headers = {
            **self.HEADERS,
            "Referer": "https://www.douyin.com/",
        }
        try:
            resp = await self.client.get(
                "https://www.douyin.com/aweme/v1/web/user/profile/self/",
                headers=headers,
                cookies=cookies,
            )
            if resp.status_code != 200:
                logger.warning(f"Douyin user info API returned {resp.status_code}")
                return None
            data = resp.json()
            if data.get("status_code") != 0:
                logger.warning(f"Douyin user info failed: {data.get('status_msg', 'unknown')}")
                return None
            user = data.get("user", {})
            return {
                "uid": user.get("uid") or user.get("short_id"),
                "nickname": user.get("nickname", ""),
                "avatar": user.get("avatar_thumb", {}).get("url_list", [""])[0] if user.get("avatar_thumb") else "",
            }
        except Exception as e:
            logger.warning(f"Douyin user info request error: {e}")
            return None

    async def _fetch_videos_by_tab_interception(
        self,
        cookie_str: str,
        tab_url: str,
        api_patterns: tuple[str, ...],
        max_count: int,
        label: str = "videos",
    ) -> list[dict]:
        """Open a Douyin self-page tab and intercept the aweme_list API responses
        that the page itself fires.

        Douyin's own JavaScript signs every request (X-Bogus / a_bogus / msToken),
        so intercepting the in-page traffic avoids the brittle client-side
        signature reimplementations that fail on the favorite/collect endpoints.
        """
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            logger.error("[Douyin] Playwright not installed")
            return []

        captured: list[dict] = []
        seen_ids: set[str] = set()
        done = asyncio.Event()
        # Track whether the last intercepted response indicated more data is
        # available. Updated by _on_response; consumed by the scroll loop to
        # decide whether to keep paginating.
        state = {"has_more": True}

        async def _on_response(response):
            if done.is_set():
                return
            url = response.url
            if not any(p in url for p in api_patterns):
                return
            try:
                body = await response.json()
            except Exception:
                return
            aweme_list = body.get("aweme_list") or []
            # Reflect server-side pagination state even on empty pages so the
            # scroll loop can terminate as soon as the collection is exhausted.
            state["has_more"] = bool(body.get("has_more", False))
            if not aweme_list:
                return
            for item in aweme_list:
                if len(captured) >= max_count:
                    done.set()
                    return
                aid = str(item.get("aweme_id", ""))
                if not aid or aid in seen_ids:
                    continue
                seen_ids.add(aid)
                captured.append(self._aweme_to_dict(item))
            logger.info(f"[Douyin] {label}: intercepted {len(captured)}/{max_count} videos so far (has_more={state['has_more']})")

        p, browser, context, page = await self._launch_douyin_context(cookie_str)
        try:
            page.on("response", _on_response)

            try:
                logger.info(f"[Douyin] {label}: opening {tab_url}")
                # 收藏 tab 页面较重（HTML ~800KB），domcontentloaded+30s 在慢网络下会超时，
                # 导致页面半加载、后续 page.evaluate 挂起。改用 load 事件 + 60s 超时，
                # 诊断验证该组合可稳定完成导航并触发 listcollection API。
                try:
                    await page.goto(tab_url, wait_until="load", timeout=60000)
                except Exception as e:
                    logger.warning(f"[Douyin] {label}: navigation warning: {e}")
                # 即使导航超时也等待页面进入稳定态，避免 evaluate 在页面仍加载时挂起
                try:
                    await page.wait_for_load_state("load", timeout=30000)
                except Exception:
                    pass
                await asyncio.sleep(4)

                # 预检 cookie 登录态：tab_url 已在 douyin.com 域名下，
                # 可直接 page.evaluate fetch user/profile/self。
                # 服务端未识别登录态时抛 DouyinCookieInvalidError，
                # 避免后续 scroll 循环空跑（页面 JS 不会发起 listcollection API）。
                await self._check_cookie_login_state(page)

                # Scroll to trigger lazy-loaded pages until we hit max_count or
                # run dry. The collection tab renders inside an inner scrollable
                # container, so scrolling window alone does not trigger the
                # page's own pagination. We scroll every scrollable element that
                # is taller than its viewport, plus the window as a fallback.
                dry_runs = 0
                # max_count ~ N videos / ~7 per page => generous upper bound.
                for _ in range(max(60, max_count // 3 + 20)):
                    if done.is_set():
                        break
                    if not state["has_more"]:
                        # Server already told us there is no more data; stop.
                        break
                    before = len(captured)
                    try:
                        # 加 10s 超时保护：页面未就绪或卡住时 evaluate 可能无限挂起，
                        # 超时后跳过本轮滚动，由 dry_runs 计数决定是否终止
                        await asyncio.wait_for(
                            page.evaluate(
                                "() => {"
                                "  const els = document.querySelectorAll("
                                "    '[class*=\"list\"],[class*=\"scroll\"],[class*=\"container\"],[class*=\"tab\"]');"
                                "  let hit = false;"
                                "  els.forEach(el => {"
                                "    if (el.scrollHeight > el.clientHeight + 50) {"
                                "      el.scrollTop = el.scrollHeight;"
                                "      hit = true;"
                                "    }"
                                "  });"
                                "  if (!hit) window.scrollTo(0, document.body.scrollHeight);"
                                "}"
                            ),
                            timeout=10,
                        )
                    except asyncio.TimeoutError:
                        logger.debug(f"[Douyin] {label}: scroll evaluate timed out, skipping")
                    except Exception:
                        pass
                    # 缩短滚动等待到 1.2 秒，加快分页触发
                    await asyncio.sleep(1.2)
                    if len(captured) >= max_count:
                        done.set()
                        break
                    if len(captured) == before:
                        dry_runs += 1
                        # 抖音收藏分页加载较慢，需要更多重试才能到达用户设定的上限
                        if dry_runs >= 20:
                            break
                    else:
                        dry_runs = 0
            finally:
                done.set()
                try:
                    page.remove_listener("response", _on_response)
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception as e:
                    logger.debug(f"Douyin cleanup error: {e}")
                try:
                    await browser.close()
                except Exception as e:
                    logger.debug(f"Douyin browser cleanup error: {e}")
                try:
                    await p.stop()
                except Exception as e:
                    logger.debug(f"Douyin playwright cleanup error: {e}")
        finally:
            pass

        logger.info(f"[Douyin] {label}: captured {len(captured)} videos via interception")
        return captured[:max_count]

    async def fetch_favorite_videos_via_browser(self, cookie_str: str, max_count: int = 50) -> list[dict]:
        """Fetch Douyin favorites via Playwright browser evaluate.
        Uses page.evaluate to call the API from within douyin.com page context,
        so cookies + TLS fingerprint + Origin/Referer are all authentic.
        """
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            logger.error("[Douyin] Playwright 未安装，请运行 pip install playwright && playwright install chromium")
            raise RuntimeError("[Douyin] Playwright 未安装，请运行 pip install playwright && playwright install chromium")

        videos = []
        p, browser, context, page = await self._launch_douyin_context(cookie_str)
        try:
            try:
                # Visit douyin.com to validate cookies
                logger.info("[Douyin] Browser warming up session on douyin.com...")
                await page.goto("https://www.douyin.com/",
                               wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(1.5)

                # 预检 cookie 登录态：服务端返回未登录时直接抛异常，
                # 避免后续拿空 aweme_list 误报"无喜欢视频"
                await self._check_cookie_login_state(page)

                # Call favorites API from inside the browser context
                logger.info("[Douyin] Calling favorites API via page.evaluate...")
                cursor = 0
                has_more = True
                empty_retries = 0  # 连续空响应重试计数

                while has_more and len(videos) < max_count:
                    cursor_val = cursor
                    params_entries = ",".join(
                        f"['{k}','{v}']" for k, v in DOUYIN_API_PARAMS.items()
                    )
                    js = (
                        "(async () => {"
                        "  const p = new URLSearchParams([" + params_entries + ","
                        "    ['count','20'],['max_cursor','" + str(cursor_val) + "']]);"
                        "  const u = 'https://www.douyin.com/aweme/v1/web/aweme/favorite/?' + p.toString();"
                        "  const r = await fetch(u, { credentials: 'include' });"
                        "  return await r.json();"
                        "})()"
                    )
                    result = await page.evaluate(js)

                    if not result:
                        logger.warning("[Douyin] Browser fetch returned None")
                        break

                    sc = result.get("status_code")
                    sm = result.get("status_msg", "")
                    logger.info(f"[Douyin] Browser API: status_code={sc}, msg={sm}, videos={len(videos)}/{max_count}")

                    if sc not in (0, None):
                        logger.warning(f"[Douyin] Browser API error: {sm}")
                        break

                    aweme_list = result.get("aweme_list") or []
                    if not aweme_list:
                        # 空列表但 has_more=True 时，可能是抖音分页间隙，重试而非直接退出
                        empty_retries += 1
                        if empty_retries >= 3:
                            logger.info(f"[Douyin] Empty aweme_list after {empty_retries} retries, stopping")
                            break
                        logger.info(f"[Douyin] Empty aweme_list, retry {empty_retries}/3 (cursor={cursor_val})")
                        await asyncio.sleep(1.5)
                        continue
                    empty_retries = 0

                    for item in aweme_list:
                        videos.append(self._aweme_to_dict(item))

                    has_more = result.get("has_more", False)
                    cursor = result.get("max_cursor", 0)
                    if not has_more:
                        break
                    # 请求间隔，降低风控触发概率
                    await asyncio.sleep(0.5)

            finally:
                try:
                    await context.close()
                except Exception as e:
                    logger.debug(f"Douyin cleanup error: {e}")
                try:
                    await browser.close()
                except Exception as e:
                    logger.debug(f"Douyin browser cleanup error: {e}")
                try:
                    await p.stop()
                except Exception as e:
                    logger.debug(f"Douyin playwright cleanup error: {e}")
        finally:
            pass

        logger.info(f"[Douyin] Browser favorites: {len(videos)} videos")
        return videos

    async def fetch_collected_videos_via_browser(self, cookie_str: str, max_count: int = 50) -> list[dict]:
        """Fetch the user's collected (收藏) Douyin videos.

        Primary: tab interception. The collection endpoint
        (/aweme/v1/web/aweme/listcollection/) is signed by Douyin's own
        in-page JS with a_bogus + msToken + x-secsdk-web-signature, which
        cannot be reproduced from a plain page.evaluate fetch (the manual
        XBogus-signed call is rejected with "Unsupported path(Janus)").
        Intercepting the page's own signed requests while scrolling the
        collection tab's inner container reliably paginates to max_count.

        注意：DouyinCookieInvalidError 必须向上传播，不能被 except Exception 吞掉，
        否则 sync_favorites 的 cookie 失效检测会失效，误报"无收藏视频"。
        """
        try:
            videos = await self._fetch_videos_by_tab_interception(
                cookie_str,
                tab_url="https://www.douyin.com/user/self?showTab=favorite_collection",
                api_patterns=("/aweme/v1/web/aweme/listcollection/", "/aweme/v1/web/collects/"),
                max_count=max_count,
                label="collects",
            )
            if videos:
                return videos
        except DouyinCookieInvalidError:
            # cookie 失效必须向上传播，让 sync_favorites 返回明确错误
            raise
        except Exception as e:
            logger.warning(f"[Douyin] collects interception error: {e}")
        logger.warning("[Douyin] collects interception empty, falling back to flat API")
        try:
            return await self.fetch_collected_flat_videos_via_browser(cookie_str, max_count)
        except DouyinCookieInvalidError:
            # cookie 失效必须向上传播，让 sync_favorites 返回明确错误
            raise
        except Exception as e:
            # 主路径与 fallback 均失败时不再静默返回空列表，抛出异常让
            # _sync_favorites_task 的 browser_error 检测能正确捕获，避免误报"无收藏视频"。
            logger.warning(f"[Douyin] flat collect fetch error: {e}")
            raise

    async def fetch_collected_flat_videos_via_browser(self, cookie_str: str, max_count: int = 50) -> list[dict]:
        """Fetch Douyin collected videos via Playwright page.evaluate + direct API call.
        Calls /aweme/v1/web/aweme/listcollection/ with XBogus-signed URL.
        Same pattern as fetch_favorite_videos_via_browser.
        """
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            logger.error("[Douyin] Playwright not installed")
            return []

        videos = []
        p, browser, context, page = await self._launch_douyin_context(cookie_str)
        try:
            try:
                # Warm up session
                logger.info("[Douyin] Flat collect: warming up session...")
                await page.goto("https://www.douyin.com/",
                               wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(1.5)

                # 预检 cookie 登录态：未登录时直接抛异常，避免后续 listcollection API 空跑
                await self._check_cookie_login_state(page)

                # Call listcollection API from inside browser context
                logger.info("[Douyin] Flat collect: calling listcollection API via page.evaluate...")
                cursor = 0
                has_more = True

                # 使用 XBogus 签名；listcollection 必须带 aweme_type=0（视频）
                xb = XBogus()
                while has_more and len(videos) < max_count:
                    cursor_val = cursor
                    flat_params = dict(DOUYIN_API_PARAMS)
                    flat_params["cursor"] = str(cursor_val)
                    flat_params["count"] = "20"
                    flat_params["aweme_type"] = "0"  # 0=视频, 缺少此参数直接返回 Unsupported
                    query_str = "&".join(f"{k}={v}" for k, v in flat_params.items())
                    signed_url, _xb2, ua = xb.build(f"/aweme/v1/web/aweme/listcollection/?{query_str}")
                    full_url = f"https://www.douyin.com{signed_url}"
                    logger.info(f"[Douyin] Flat collect: fetching page cursor={cursor_val} (aweme_type=0)...")
                    js = (
                        "(async () => {"
                        "  const r = await fetch('" + full_url + "', {"
                        "    credentials: 'include',"
                        "    headers: { 'Referer': 'https://www.douyin.com/', 'User-Agent': '" + ua + "' }"
                        "  });"
                        "  const text = await r.text();"
                        "  try { return JSON.parse(text); }"
                        "  catch(e) { return {_error: true, _text: text.substring(0,200)}; }"
                        "})()"
                    )
                    result = await page.evaluate(js)

                    if not result:
                        logger.warning("[Douyin] Flat collect: fetch returned None")
                        break

                    if result.get("_error"):
                        logger.error(f"[Douyin] Flat collect: non-JSON response: {result.get('_text','')}")
                        break

                    sc = result.get("status_code")
                    sm = result.get("status_msg", "")
                    logger.info(f"[Douyin] Flat collect: status_code={sc}, msg={sm}, keys={list(result.keys())}")

                    if sc not in (0, None):
                        logger.warning(f"[Douyin] Flat collect API error (status_code={sc}): {sm}")
                        if sc == 4018:
                            logger.warning("[Douyin] Flat collect: 风控/签名/msToken 问题 (4018)")
                        break

                    aweme_list = result.get("aweme_list") or []
                    if not aweme_list:
                        break

                    for item in aweme_list:
                        videos.append(self._aweme_to_dict(item))

                    has_more = result.get("has_more", False)
                    cursor = result.get("cursor", result.get("max_cursor", 0))
                    logger.info(f"[Douyin] Flat collect: {len(videos)}/{max_count}, has_more={has_more}")
                    if not has_more:
                        break

            finally:
                try:
                    await context.close()
                except Exception as e:
                    logger.debug(f"Douyin cleanup error: {e}")
                try:
                    await browser.close()
                except Exception as e:
                    logger.debug(f"Douyin browser cleanup error: {e}")
                try:
                    await p.stop()
                except Exception as e:
                    logger.debug(f"Douyin playwright cleanup error: {e}")
        finally:
            pass

        logger.info(f"[Douyin] Flat collect: {len(videos)} total videos")
        return videos

    #  Cleanup
    # ------------------------------------------------------------------

    async def close(self):
        await self.client.aclose()
        if self.use_browser_pool:
            return
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
