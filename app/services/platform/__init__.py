"""
ClipMind Platform Abstraction Layer

Provides a unified interface for different video platforms (Bilibili, Douyin, etc.).
"""
from app.services.platform.base import BasePlatformService
from app.services.platform.bilibili import BilibiliPlatformService
from app.services.platform.douyin import DouyinPlatformService

__all__ = ["BasePlatformService", "BilibiliPlatformService", "DouyinPlatformService"]
