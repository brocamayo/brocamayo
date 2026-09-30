from __future__ import annotations

from ..config import Config
from ..queue import Target
from .base import NotLoggedIn, PublishError, PublishResult, Publisher
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .x import XPublisher
from .youtube import YouTubePublisher

PUBLISHERS: dict[str, type[Publisher]] = {
    "youtube": YouTubePublisher,
    "instagram": InstagramPublisher,
    "tiktok": TikTokPublisher,
    "x": XPublisher,
}


def get_publisher(target: Target, config: Config) -> Publisher:
    return PUBLISHERS[target.platform](config, target.account)


__all__ = ["PUBLISHERS", "NotLoggedIn", "PublishError", "PublishResult", "Publisher", "get_publisher"]
