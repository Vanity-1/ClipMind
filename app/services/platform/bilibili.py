"""
ClipMind Platform Abstraction Layer

Bilibili platform service -- wraps existing BilibiliService and ContentFetcher.
"""
from typing import Optional
from app.models import VideoContent
from app.services.platform.base import BasePlatformService
from app.services.bilibili import BilibiliService
from app.services.content_fetcher import ContentFetcher


class BilibiliPlatformService(BasePlatformService):
    """Bilibili platform adapter implementing BasePlatformService."""

    platform = "bilibili"

    def __init__(
        self,
        sessdata: str = None,
        bili_jct: str = None,
        dedeuserid: str = None,
        use_browser_pool: bool = True,
        qrcode_timeout: int = 10,
        qrcode_retries: int = 2,
    ):
        self.bili = BilibiliService(
            sessdata=sessdata,
            bili_jct=bili_jct,
            dedeuserid=dedeuserid,
            use_browser_pool=use_browser_pool,
            qrcode_timeout=qrcode_timeout,
            qrcode_retries=qrcode_retries,
        )

    async def get_video_info(self, video_id: str) -> Optional[dict]:
        """Get Bilibili video info by BV number."""
        return await self.bili.get_video_info(video_id)

    async def fetch_content(
        self,
        video_id: str,
        asr_service,
        cancel_check=None,
    ) -> VideoContent:
        """Fetch Bilibili video content using existing ContentFetcher pipeline."""
        fetcher = ContentFetcher(self.bili, asr_service, cancel_check=cancel_check)
        return await fetcher.fetch_content(video_id)

    async def get_audio_url(self, video_id: str, cid: int = None) -> Optional[str]:
        """Get Bilibili audio stream URL."""
        return await self.bili.get_audio_url(video_id, cid)

    async def download_audio_to_file(self, audio_url: str, file_path: str) -> bool:
        """Download Bilibili audio stream to file."""
        return await self.bili.download_audio_to_file(audio_url, file_path)

    async def close(self):
        """Close the underlying Bilibili HTTP client."""
        await self.bili.close()

    # --- Bilibili-specific methods (not in base) ---

    async def generate_qrcode(self) -> dict:
        return await self.bili.generate_qrcode()

    async def poll_qrcode_status(self, qrcode_key: str) -> dict:
        return await self.bili.poll_qrcode_status(qrcode_key)

    async def get_nav_info(self) -> Optional[dict]:
        return await self.bili.get_nav_info()

    async def get_favorite_folders(self, up_mid: int = None) -> list:
        return await self.bili.get_favorite_folders(up_mid)

    async def get_folder_videos(self, **kwargs):
        return await self.bili.get_folder_videos(**kwargs)

    async def get_all_folder_videos(self, media_id: int) -> dict:
        return await self.bili.get_all_folder_videos(media_id)

    def _get_cookies(self):
        return self.bili._get_cookies()
