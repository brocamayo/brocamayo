from __future__ import annotations

import json
import logging
import os
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


class NotLoggedIn(PublishError):
    """No (or expired) login for this account; waits without using up retries."""


@dataclass
class PublishResult:
    remote_id: str
    url: str | None = None


class Publisher:
    name = ""

    def __init__(self, config: Config, account: str, session: requests.Session | None = None):
        self.config = config
        self.account = account
        self.settings = config.settings(account, self.name)
        self.session = session or requests.Session()

    def token_file(self) -> Path:
        return self.config.path(self.settings["token_file"])

    def publish(self, post: Post, video: Path) -> PublishResult:  # pragma: no cover
        raise NotImplementedError

    # --- helpers ----------------------------------------------------------

    def request(self, method: str, url: str, *, retries: int = 3, creates: bool = False,
                **kwargs: Any) -> requests.Response:
        """HTTP call with backoff on rate limits and server errors.

        creates=True marks a call that makes the post go live: it is only retried on
        HTTP 429 (definitely not processed), never on errors that might have gone through,
        so a flaky connection can't double-post.
        """
        kwargs.setdefault("timeout", 120)
        retry_on = {429} if creates else RETRY_STATUSES
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == retries or creates:
                    raise PublishError(f"{self.name}: network error calling {url.split('?')[0]}: {exc}") from exc
                resp = None
            if resp is not None and (resp.status_code not in retry_on or attempt == retries):
                return resp
            delay = 2 ** (attempt + 1)
            reason = "network error" if resp is None else f"HTTP {resp.status_code}"
            log.warning("%s: %s on %s, retrying in %ss", self.name, reason, url.split("?")[0], delay)
            time.sleep(delay)
        raise AssertionError("unreachable")


def command_name() -> str:
    """How the user runs socialq: the setup scripts' shortcuts set SOCIALQ_CMD."""
    return os.environ.get("SOCIALQ_CMD", "python -m socialq")


def auth_hint(platform: str, account: str, config: Config) -> str:
    flag = f" --account {account}" if len(config.accounts) > 1 else ""
    return f"{command_name()} auth {platform}{flag}"


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
