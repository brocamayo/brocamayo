"""Finds posts that are due and publishes them to each platform."""

from __future__ import annotations

import logging
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import requests

from .config import Config
from .platforms import PublishError, Publisher, get_publisher
from .queue import Post, load_queue, validate
from .state import State

log = logging.getLogger("socialq")


@dataclass
class Outcome:
    post_id: str
    platform: str
    ok: bool
    detail: str


def due_work(posts: list[Post], state: State, config: Config, now: datetime) -> list[tuple[Post, str]]:
    """(post, platform) pairs that should be attempted right now."""
    work = []
    for post in posts:
        if post.publish_at > now:
            continue
        for platform in post.platforms:
            if not config.enabled(platform) or state.is_published(post.id, platform):
                continue
            if state.attempts(post.id, platform) >= config.max_attempts:
                continue
            failed_at = state.get(post.id, platform).get("failed_at")
            retry_after = timedelta(minutes=config.retry_delay_minutes)
            if failed_at and datetime.fromisoformat(failed_at) + retry_after > now:
                continue
            work.append((post, platform))
    return work


def run_once(
    config: Config,
    now: datetime | None = None,
    dry_run: bool = False,
    publisher_factory: Callable[[str, Config], Publisher] = get_publisher,
) -> list[Outcome]:
    now = now or datetime.now(config.timezone)
    state = State(config.state_file)
    posts = load_queue(config)
    work = due_work(posts, state, config, now)
    if not work:
        log.info("Nothing due.")
        return []

    outcomes: list[Outcome] = []
    publishers: dict[str, Publisher] = {}
    for post, platform in work:
        label = f"{post.id} -> {platform}"
        problems = validate(post, config)
        if problems:
            msg = "; ".join(problems)
            log.error("%s: skipped, %s", label, msg)
            if not dry_run:
                state.record_failure(post.id, platform, msg, at=now)
            outcomes.append(Outcome(post.id, platform, False, msg))
            continue
        if dry_run:
            log.info("[dry run] would publish %s", label)
            outcomes.append(Outcome(post.id, platform, True, "dry run"))
            continue

        log.info("Publishing %s (scheduled %s)", label, post.publish_at.strftime("%Y-%m-%d %H:%M"))
        try:
            publisher = publishers.get(platform) or publishers.setdefault(
                platform, publisher_factory(platform, config))
            result = publisher.publish(post, post.video_path(config))
        except PublishError as exc:
            state.record_failure(post.id, platform, str(exc), at=now)
            log.error("%s failed: %s", label, exc)
            outcomes.append(Outcome(post.id, platform, False, str(exc)))
            continue
        except Exception as exc:  # keep going so one bad platform doesn't block the others
            state.record_failure(post.id, platform, f"{type(exc).__name__}: {exc}", at=now)
            log.error("%s crashed:\n%s", label, traceback.format_exc())
            outcomes.append(Outcome(post.id, platform, False, f"{type(exc).__name__}: {exc}"))
            continue
        state.record_success(post.id, platform, result.remote_id, result.url, at=now)
        log.info("%s done: %s", label, result.url or result.remote_id)
        outcomes.append(Outcome(post.id, platform, True, result.url or result.remote_id))

    if not dry_run:
        notify(outcomes)
    return outcomes


def notify(outcomes: list[Outcome]) -> None:
    """Optional: send a summary to a Slack/Discord/Teams webhook (NOTIFY_WEBHOOK_URL)."""
    url = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not url or not outcomes:
        return
    lines = [f"{'✅' if o.ok else '❌'} {o.post_id} → {o.platform}: {o.detail}" for o in outcomes]
    text = "socialq run\n" + "\n".join(lines)
    try:
        # "text" is read by Slack/Teams, "content" by Discord.
        requests.post(url, json={"text": text, "content": text[:2000]}, timeout=15)
    except requests.RequestException as exc:
        log.warning("Couldn't send notification: %s", exc)
