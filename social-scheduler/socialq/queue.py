"""The post queue: a YAML list of videos, when to post them, and what to say."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .config import PLATFORMS, Config

# Platform limits, used by `socialq check` before anything is uploaded.
YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 5000
INSTAGRAM_CAPTION_MAX = 2200
INSTAGRAM_HASHTAG_MAX = 30
TIKTOK_CAPTION_MAX = 2200

DATETIME_FORMAT = "%Y-%m-%d %H:%M"


@dataclass
class Post:
    id: str
    video: str
    publish_at: datetime
    platforms: list[str]
    title: str = ""
    caption: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail: str | None = None
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def video_path(self, config: Config) -> Path:
        p = Path(self.video).expanduser()
        return p if p.is_absolute() else config.media_dir / p

    def thumbnail_path(self, config: Config) -> Path | None:
        if not self.thumbnail:
            return None
        p = Path(self.thumbnail).expanduser()
        return p if p.is_absolute() else config.media_dir / p

    def option(self, platform: str, key: str, default: Any = None) -> Any:
        return self.overrides.get(platform, {}).get(key, default)

    # --- per-platform text -------------------------------------------------

    def hashtags(self) -> str:
        return " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in self.tags)

    def social_caption(self, platform: str) -> str:
        """Caption for Instagram/TikTok: an override wins, else caption + #tags."""
        override = self.option(platform, "caption")
        if override is not None:
            return str(override).strip()
        parts = [self.caption.strip()]
        tags = self.hashtags()
        if tags and tags not in self.caption:
            parts.append(tags)
        return "\n\n".join(p for p in parts if p)

    def youtube_title(self) -> str:
        return str(self.option("youtube", "title", self.title)).strip()

    def youtube_description(self) -> str:
        return str(self.option("youtube", "description", self.caption)).strip()

    def youtube_tags(self) -> list[str]:
        return [t.lstrip("#") for t in self.option("youtube", "tags", self.tags)]


def parse_datetime(value: Any, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        raise ValueError(f"publish_at {value!r} needs a time, e.g. '{value} 17:00'")
    else:
        text = str(value).strip().replace("T", " ")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"Couldn't read publish_at {value!r}; use 'YYYY-MM-DD HH:MM'"
            ) from None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "post"


def post_from_dict(raw: dict[str, Any], config: Config) -> Post:
    if not raw.get("video"):
        raise ValueError(f"Queue entry is missing 'video': {raw}")
    if not raw.get("publish_at"):
        raise ValueError(f"Queue entry for {raw['video']} is missing 'publish_at'")
    platforms = raw.get("platforms") or [p for p in PLATFORMS if config.enabled(p)]
    unknown = set(platforms) - set(PLATFORMS)
    if unknown:
        raise ValueError(f"Unknown platform(s) {sorted(unknown)} in {raw['video']}")
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\s]+", tags) if t]
    return Post(
        id=str(raw.get("id") or _slug(Path(raw["video"]).stem)),
        video=str(raw["video"]),
        publish_at=parse_datetime(raw["publish_at"], config.timezone),
        platforms=list(platforms),
        title=str(raw.get("title") or ""),
        caption=str(raw.get("caption") or ""),
        tags=[str(t) for t in tags],
        thumbnail=raw.get("thumbnail"),
        overrides={p: dict(raw.get(p) or {}) for p in PLATFORMS if raw.get(p)},
    )


def load_queue(config: Config) -> list[Post]:
    if not config.queue_file.exists():
        return []
    raw = yaml.safe_load(config.queue_file.read_text()) or []
    if isinstance(raw, dict):  # also accept `posts: [...]`
        raw = raw.get("posts") or []
    posts = [post_from_dict(item, config) for item in raw]
    seen: set[str] = set()
    for post in posts:
        if post.id in seen:
            raise ValueError(f"Duplicate post id {post.id!r} in {config.queue_file}")
        seen.add(post.id)
    return sorted(posts, key=lambda p: p.publish_at)


def append_to_queue(config: Config, entry: dict[str, Any]) -> None:
    """Append one entry without rewriting (and losing comments in) the file."""
    path = config.queue_file
    text = path.read_text() if path.exists() else ""
    existing = yaml.safe_load(text) if text.strip() else None
    if isinstance(existing, dict):
        raise ValueError(
            f"{path} uses the 'posts:' layout; add entries by hand or convert it to a top-level list"
        )
    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=1000)
    if not existing:
        # Keep any leading comments, drop an explicit empty list.
        kept = "\n".join(l for l in text.splitlines() if l.strip().startswith("#"))
        path.write_text((kept + "\n\n" if kept else "") + block)
    else:
        path.write_text(text.rstrip("\n") + "\n\n" + block)


def validate(post: Post, config: Config) -> list[str]:
    """Return human-readable problems that would make a publish fail."""
    problems: list[str] = []
    video = post.video_path(config)
    if not video.exists():
        problems.append(f"video file not found: {video}")
    thumb = post.thumbnail_path(config)
    if thumb and not thumb.exists():
        problems.append(f"thumbnail not found: {thumb}")
    for platform in post.platforms:
        if not config.enabled(platform):
            problems.append(f"{platform} is listed but not enabled in config.yaml")
    if "youtube" in post.platforms:
        title = post.youtube_title()
        if not title:
            problems.append("youtube needs a title")
        if len(title) > YOUTUBE_TITLE_MAX:
            problems.append(f"youtube title is {len(title)} chars (max {YOUTUBE_TITLE_MAX})")
        if "<" in title or ">" in title:
            problems.append("youtube title can't contain < or >")
        desc = post.youtube_description()
        if len(desc.encode()) > YOUTUBE_DESCRIPTION_MAX:
            problems.append(f"youtube description is over {YOUTUBE_DESCRIPTION_MAX} bytes")
    if "instagram" in post.platforms:
        caption = post.social_caption("instagram")
        if len(caption) > INSTAGRAM_CAPTION_MAX:
            problems.append(f"instagram caption is {len(caption)} chars (max {INSTAGRAM_CAPTION_MAX})")
        if caption.count("#") > INSTAGRAM_HASHTAG_MAX:
            problems.append(f"instagram allows at most {INSTAGRAM_HASHTAG_MAX} hashtags")
    if "tiktok" in post.platforms:
        caption = post.social_caption("tiktok")
        if len(caption) > TIKTOK_CAPTION_MAX:
            problems.append(f"tiktok caption is {len(caption)} chars (max {TIKTOK_CAPTION_MAX})")
    return problems
