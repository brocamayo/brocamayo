"""Remembers what has been posted where, so nothing is ever posted twice."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PUBLISHED = "published"
FAILED = "failed"


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"posts": {}}
        if path.exists():
            self.data = json.loads(path.read_text() or "{}") or {"posts": {}}
            self.data.setdefault("posts", {})

    def get(self, post_id: str, platform: str) -> dict[str, Any]:
        return self.data["posts"].get(post_id, {}).get(platform, {})

    def is_published(self, post_id: str, platform: str) -> bool:
        return self.get(post_id, platform).get("status") == PUBLISHED

    def attempts(self, post_id: str, platform: str) -> int:
        return int(self.get(post_id, platform).get("attempts", 0))

    def _entry(self, post_id: str, platform: str) -> dict[str, Any]:
        return self.data["posts"].setdefault(post_id, {}).setdefault(platform, {})

    def record_success(self, post_id: str, platform: str, remote_id: str, url: str | None,
                       at: datetime | None = None) -> None:
        entry = self._entry(post_id, platform)
        entry.update(
            status=PUBLISHED,
            remote_id=remote_id,
            url=url,
            published_at=_iso(at),
            attempts=entry.get("attempts", 0) + 1,
            last_error=None,
        )
        self.save()

    def record_failure(self, post_id: str, platform: str, error: str, at: datetime | None = None) -> None:
        entry = self._entry(post_id, platform)
        entry.update(
            status=FAILED,
            attempts=entry.get("attempts", 0) + 1,
            last_error=error,
            failed_at=_iso(at),
        )
        self.save()

    def reset(self, post_id: str, platform: str | None = None) -> None:
        posts = self.data["posts"]
        if platform is None:
            posts.pop(post_id, None)
        else:
            posts.get(post_id, {}).pop(platform, None)
        self.save()

    def save(self) -> None:
        # Write-then-rename so a crash mid-write can't corrupt the history.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)


def _iso(at: datetime | None) -> str:
    return (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path, stale_after_s: int = 3 * 3600) -> Iterator[None]:
    """Stop two scheduled runs from uploading the same video at once."""
    if path.exists() and time.time() - path.stat().st_mtime > stale_after_s:
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise AlreadyRunning(
            f"Another run is in progress ({path} exists). Delete it if that's not true."
        ) from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
