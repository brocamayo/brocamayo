from __future__ import annotations

from ..config import Config
from .base import PublishError, PublishResult, Publisher
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher

PUBLISHERS: dict[str, type[Publisher]] = {
    "youtube": YouTubePublisher,
    "instagram": InstagramPublisher,
    "tiktok": TikTokPublisher,
}


def get_publisher(name: str, config: Config) -> Publisher:
    return PUBLISHERS[name](config)


__all__ = ["PUBLISHERS", "PublishError", "PublishResult", "Publisher", "get_publisher"]
