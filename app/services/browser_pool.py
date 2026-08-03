"""浏览器实例池 - 复用浏览器实例以提升性能"""

import asyncio
from loguru import logger
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from typing import Optional


class BrowserPool:
    """浏览器实例池（单例）"""

    _instance: Optional["BrowserPool"] = None
    _lock = asyncio.Lock()

    def __new__(cls) -> "BrowserPool":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._browser: Optional[Browser] = None
            cls._instance._playwright = None
            cls._instance._contexts: dict[str, BrowserContext] = {}
            cls._instance._initialized = False
        return cls._instance

    async def initialize(self) -> None:
        """初始化浏览器实例（懒加载）"""
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            logger.info("初始化浏览器池...")
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-gpu",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-extensions",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                self._initialized = True
                logger.info("浏览器池初始化完成")
            except Exception as e:
                logger.error(f"浏览器池初始化失败: {e}")
                if self._playwright:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                self._browser = None
                raise

    async def _ensure_browser_alive(self) -> None:
        """检查浏览器实例是否仍然存活，断连时自动重建"""
        if self._browser is not None:
            try:
                if not self._browser.is_connected():
                    logger.warning("浏览器实例已断开连接，正在重建...")
                    self._browser = None
                    self._contexts.clear()
                    self._initialized = False
                    await self.initialize()
            except Exception:
                self._browser = None
                self._contexts.clear()
                self._initialized = False
                await self.initialize()

    async def get_context(self, platform: str) -> BrowserContext:
        """获取指定平台的浏览器上下文"""
        await self.initialize()
        await self._ensure_browser_alive()
        if platform in self._contexts:
            # 安全检查 context 是否仍然有效
            # Playwright BrowserContext 没有 is_closed() 方法
            try:
                _ = self._contexts[platform].pages
            except Exception:
                del self._contexts[platform]
                self._contexts[platform] = await self._browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="zh-CN",
                )
                logger.info(f"重建浏览器上下文: {platform}")
        else:
            self._contexts[platform] = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            logger.info(f"创建新的浏览器上下文: {platform}")
        return self._contexts[platform]

    async def get_page(self, platform: str) -> Page:
        """获取指定平台的页面

        注意：不再拦截 image/media/font/stylesheet 资源。
        之前拦截这些资源会导致抖音等页面无法正常加载（domcontentloaded 都触发不了），
        登录弹窗和 QR 码无法渲染。让页面完整加载虽然慢几秒，但能确保功能正常。
        """
        await self.initialize()
        await self._ensure_browser_alive()
        context = await self.get_context(platform)
        page = await context.new_page()
        return page

    async def close(self) -> None:
        """关闭所有浏览器实例"""
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.debug(f"关闭浏览器异常: {e}")
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.debug(f"停止 playwright 异常: {e}")
            self._playwright = None
        self._initialized = False
        self._contexts.clear()
        logger.info("浏览器池已关闭")

    async def cleanup_contexts(self) -> None:
        """清理已关闭的上下文"""
        closed = []
        for k, v in list(self._contexts.items()):
            try:
                _ = v.pages
            except Exception:
                closed.append(k)
        for k in closed:
            del self._contexts[k]
        if closed:
            logger.info(f"清理了 {len(closed)} 个已关闭的上下文")

    @property
    def is_initialized(self) -> bool:
        return self._initialized


browser_pool = BrowserPool()