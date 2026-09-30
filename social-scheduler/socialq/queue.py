"""The post queue: a YAML list of videos, when to post them, and what to say."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
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
X_TEXT_MAX = 280            # accounts with X Premium can raise this with x.max_chars
X_URL_LENGTH = 23           # X counts every link as 23 characters

DATETIME_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True)
class Target:
    """One place a post goes: an account on a platform, e.g. main/youtube."""
    account: str
    platform: str

    @property
    def key(self) -> str:
        return f"{self.account}/{self.platform}"

    def __str__(self) -> str:
        return self.key


@dataclass
class Post:
    id: str
    video: str
    publish_at: datetime
    accounts: list[str] | None = None        # None = every account
    platforms: list[str] | None = None       # None = every platform the account has
    title: str = ""
    caption: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail: str | None = None
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    per_account: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- where it goes -------------------------------------------------------

    def targets(self, config: Config) -> list[Target]:
        accounts = self.accounts or list(config.accounts)
        out = []
        for name in accounts:
            if name not in config.accounts:
                continue  # reported by validate()
            for platform in self.platforms or config.accounts[name].platforms:
                if config.enabled(name, platform):
                    out.append(Target(name, platform))
        return out

    def due_at(self, target: Target, config: Config) -> datetime:
        return self.publish_at + timedelta(minutes=config.accounts[target.account].delay_minutes)

    def for_account(self, account: str) -> Post:
        """This post with the account's own title/caption/tags/per-platform text applied."""
        custom = self.per_account.get(account)
        if not custom:
            return self
        overrides = copy.deepcopy(self.overrides)
        for platform in PLATFORMS:
            if custom.get(platform):
                overrides.setdefault(platform, {}).update(custom[platform])
        tags = custom.get("tags", self.tags)
        return replace(
            self,
            title=str(custom.get("title", self.title)),
            caption=str(custom.get("caption", self.caption)),
            tags=_tag_list(tags),
            thumbnail=custom.get("thumbnail", self.thumbnail),
            overrides=overrides,
            per_account={},
        )

    # --- files ---------------------------------------------------------------

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

    # --- per-platform text ---------------------------------------------------

    def hashtags(self) -> str:
        return " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in self.tags)

    def social_caption(self, platform: str) -> str:
        """Caption for Instagram/TikTok/X: an override wins, else caption + #tags."""
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


def _tag_list(tags: Any) -> list[str]:
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\s]+", tags) if t]
    return [str(t) for t in tags or []]


def _name_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    return [str(v) for v in ([value] if isinstance(value, str) else value)]


def post_from_dict(raw: dict[str, Any]) -> Post:
    if not raw.get("video"):
        raise ValueError(f"Queue entry is missing 'video': {raw}")
    if not raw.get("publish_at"):
        raise ValueError(f"Queue entry for {raw['video']} is missing 'publish_at'")
    platforms = _name_list(raw.get("platforms"))
    unknown = set(platforms or []) - set(PLATFORMS)
    if unknown:
        raise ValueError(f"Unknown platform(s) {sorted(unknown)} in {raw['video']}")
    return Post(
        id=str(raw.get("id") or _slug(Path(raw["video"]).stem)),
        video=str(raw["video"]),
        publish_at=raw["publish_at"],  # localized in load_queue
        accounts=_name_list(raw.get("accounts") or raw.get("account")),
        platforms=platforms,
        title=str(raw.get("title") or ""),
        caption=str(raw.get("caption") or ""),
        tags=_tag_list(raw.get("tags")),
        thumbnail=raw.get("thumbnail"),
        overrides={p: dict(raw.get(p) or {}) for p in PLATFORMS if raw.get(p)},
        per_account={str(k): dict(v or {}) for k, v in (raw.get("per_account") or {}).items()},
    )


def load_queue(config: Config) -> list[Post]:
    if not config.queue_file.exists():
        return []
    raw = yaml.safe_load(config.queue_file.read_text()) or []
    if isinstance(raw, dict):  # also accept `posts: [...]`
        raw = raw.get("posts") or []
    posts = []
    for item in raw:
        post = post_from_dict(item)
        post.publish_at = parse_datetime(post.publish_at, config.timezone)
        posts.append(post)
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


def x_length(text: str) -> int:
    """Approximate X's character count (links always count as 23)."""
    return len(re.sub(r"https?://\S+", "x" * X_URL_LENGTH, text))


def validate_target(post: Post, target: Target, config: Config) -> list[str]:
    """Problems that would make publishing this post to one account/platform fail."""
    post = post.for_account(target.account)
    platform, problems = target.platform, []
    if platform == "youtube":
        title = post.youtube_title()
        if not title:
            problems.append("youtube needs a title")
        if len(title) > YOUTUBE_TITLE_MAX:
            problems.append(f"youtube title is {len(title)} chars (max {YOUTUBE_TITLE_MAX})")
        if "<" in title or ">" in title:
            problems.append("youtube title can't contain < or >")
        if len(post.youtube_description().encode()) > YOUTUBE_DESCRIPTION_MAX:
            problems.append(f"youtube description is over {YOUTUBE_DESCRIPTION_MAX} bytes")
    elif platform == "instagram":
        caption = post.social_caption("instagram")
        if len(caption) > INSTAGRAM_CAPTION_MAX:
            problems.append(f"instagram caption is {len(caption)} chars (max {INSTAGRAM_CAPTION_MAX})")
        if caption.count("#") > INSTAGRAM_HASHTAG_MAX:
            problems.append(f"instagram allows at most {INSTAGRAM_HASHTAG_MAX} hashtags")
    elif platform == "tiktok":
        caption = post.social_caption("tiktok")
        if len(caption) > TIKTOK_CAPTION_MAX:
            problems.append(f"tiktok caption is {len(caption)} chars (max {TIKTOK_CAPTION_MAX})")
    elif platform == "x":
        limit = int(config.settings(target.account, "x").get("max_chars", X_TEXT_MAX))
        length = x_length(post.social_caption("x"))
        if length > limit:
            problems.append(f"x post is {length} chars (max {limit}); set a shorter x: caption")
    return [f"{target.account}: {p}" if len(config.accounts) > 1 else p for p in problems]


def validate(post: Post, config: Config) -> list[str]:
    """Human-readable problems across every place this post goes."""
    problems: list[str] = []
    video = post.video_path(config)
    if not video.exists():
        problems.append(f"video file not found: {video}")
    for name in post.accounts or []:
        if name not in config.accounts:
            problems.append(f"unknown account {name!r} (config.yaml has: {', '.join(config.accounts)})")
    for name in post.per_account:
        if name not in config.accounts:
            problems.append(f"per_account has unknown account {name!r}")
    targets = post.targets(config)
    if not targets:
        problems.append("none of its accounts have any of its platforms turned on")
    for platform in post.platforms or []:
        if not any(t.platform == platform for t in targets):
            problems.append(f"{platform} is listed but not turned on for its account(s)")
    for account in {t.account for t in targets}:
        thumb = post.for_account(account).thumbnail_path(config)
        if thumb and not thumb.exists():
            problems.append(f"thumbnail not found: {thumb}")
    for target in targets:
        problems.extend(validate_target(post, target, config))
    return list(dict.fromkeys(problems))
