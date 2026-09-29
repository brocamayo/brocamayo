from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from ..config import Config
from ..queue import Post

log = logging.getLogger("socialq")

RETRY_STATUSES = {429, 500, 502, 503, 504}


class PublishError(RuntimeError):
    """A publish failed; the message is meant to be shown to a human."""


@dataclass
class PublishResult:
    remote_id: str
    url: str | None = None


class Publisher:
    name = ""

    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.settings = config.platform(self.name)
        self.session = session or requests.Session()

    def publish(self, post: Post, video: Path) -> PublishResult:  # pragma: no cover
        raise NotImplementedError

    # --- helpers ----------------------------------------------------------

    def request(self, method: str, url: str, *, retries: int = 3, **kwargs: Any) -> requests.Response:
        """HTTP call with backoff on rate limits and server errors."""
        kwargs.setdefault("timeout", 120)
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == retries:
                    raise PublishError(f"{self.name}: network error calling {url}: {exc}") from exc
                resp = None
            if resp is not None and resp.status_code not in RETRY_STATUSES:
                return resp
            if attempt == retries:
                return resp  # type: ignore[return-value]
            delay = 2 ** (attempt + 1)
            reason = "network error" if resp is None else f"HTTP {resp.status_code}"
            log.warning("%s: %s on %s, retrying in %ss", self.name, reason, url.split("?")[0], delay)
            time.sleep(delay)
        raise AssertionError("unreachable")


def poll(check: Callable[[], bool], *, timeout_s: float, interval_s: float, what: str) -> None:
    """Call `check` until it returns True or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    while True:
        if check():
            return
        if time.monotonic() > deadline:
            raise PublishError(f"Timed out after {int(timeout_s)}s waiting for {what}")
        time.sleep(interval_s)


def error_text(resp: requests.Response) -> str:
    try:
        return json.dumps(resp.json())[:500]
    except ValueError:
        return resp.text[:500]
