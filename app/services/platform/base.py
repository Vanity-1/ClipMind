"""
ClipMind Platform Abstraction Layer

Abstract base class for video platform services.
"""
from abc import ABC, abstractmethod
from typing import Optional
from app.models import VideoContent


class BasePlatformService(ABC):
    """Abstract interface for a video platform integration."""

    platform: str  # "bilibili" | "douyin"

    @abstractmethod
    async def get_video_info(self, video_id: str) -> Optional[dict]:
        """
        Get video metadata by platform-specific video ID.

        Returns dict with keys: bvid/video_id, title, description, owner_name,
        owner_mid, duration, pic_url, cid (optional).

        Returns None if not found.
        """
        ...

    @abstractmethod
    async def fetch_content(
        self,
        video_id: str,
        asr_service,
        cancel_check=None,
    ) -> VideoContent:
        """
        Fetch content for a video (ASR transcription > subtitle > fallback).

        The platform service handles platform-specific video/audio retrieval,
        then delegates to the shared ASR pipeline.
        """
        ...

    @abstractmethod
    async def close(self):
        """Release any open connections or resources."""
        ...

    @abstractmethod
    async def download_audio_to_file(self, audio_url: str, file_path: str) -> bool:
        """Download audio stream to a local file path."""
        ...

    async def get_audio_url(self, video_id: str, cid: int = None) -> Optional[str]:
        """
        Get audio stream URL for ASR processing.
        Returns None if not available.
        """
        return None
