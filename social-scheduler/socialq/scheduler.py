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
from .platforms import NotLoggedIn, PublishError, Publisher, get_publisher
from .queue import Post, Target, load_queue, validate_target
from .state import State

log = logging.getLogger("socialq")


@dataclass
class Outcome:
    post_id: str
    target: Target
    ok: bool
    detail: str


def due_work(posts: list[Post], state: State, config: Config, now: datetime) -> list[tuple[Post, Target]]:
    """(post, account/platform) pairs that should be attempted right now."""
    work = []
    retry_after = timedelta(minutes=config.retry_delay_minutes)
    for post in posts:
        for target in post.targets(config):
            if post.due_at(target, config) > now or state.is_published(post.id, target.key):
                continue
            if state.attempts(post.id, target.key) >= config.max_attempts:
                continue
            failed_at = state.get(post.id, target.key).get("failed_at")
            if failed_at and datetime.fromisoformat(failed_at) + retry_after > now:
                continue
            work.append((post, target))
    return work


def preflight(post: Post, target: Target, config: Config) -> list[str]:
    problems = []
    if not post.video_path(config).exists():
        problems.append(f"video file not found: {post.video_path(config)}")
    thumb = post.for_account(target.account).thumbnail_path(config)
    if target.platform == "youtube" and thumb and not thumb.exists():
        problems.append(f"thumbnail not found: {thumb}")
    return problems + validate_target(post, target, config)


def run_once(
    config: Config,
    now: datetime | None = None,
    dry_run: bool = False,
    publisher_factory: Callable[[Target, Config], Publisher] = get_publisher,
) -> list[Outcome]:
    now = now or datetime.now(config.timezone)
    state = State(config.state_file)
    work = due_work(load_queue(config), state, config, now)
    if not work:
        log.info("Nothing due.")
        return []

    outcomes: list[Outcome] = []
    publishers: dict[Target, Publisher] = {}
    for post, target in work:
        label = f"{post.id} -> {target}"
        problems = preflight(post, target, config)
        if problems:
            msg = "; ".join(problems)
            log.error("%s: skipped, %s", label, msg)
            if not dry_run:
                state.record_failure(post.id, target.key, msg, at=now)
            outcomes.append(Outcome(post.id, target, False, msg))
            continue
        if dry_run:
            log.info("[dry run] would publish %s", label)
            outcomes.append(Outcome(post.id, target, True, "dry run"))
            continue

        log.info("Publishing %s (scheduled %s)", label, post.due_at(target, config).strftime("%Y-%m-%d %H:%M"))
        try:
            if target not in publishers:
                publishers[target] = publisher_factory(target, config)
            result = publishers[target].publish(post.for_account(target.account), post.video_path(config))
        except NotLoggedIn as exc:
            state.record_waiting(post.id, target.key, str(exc), at=now)
            log.warning("%s waiting: %s", label, exc)
            outcomes.append(Outcome(post.id, target, False, str(exc)))
            continue
        except PublishError as exc:
            state.record_failure(post.id, target.key, str(exc), at=now)
            log.error("%s failed: %s", label, exc)
            outcomes.append(Outcome(post.id, target, False, str(exc)))
            continue
        except Exception as exc:  # keep going so one bad platform doesn't block the others
            state.record_failure(post.id, target.key, f"{type(exc).__name__}: {exc}", at=now)
            log.error("%s crashed:\n%s", label, traceback.format_exc())
            outcomes.append(Outcome(post.id, target, False, f"{type(exc).__name__}: {exc}"))
            continue
        state.record_success(post.id, target.key, result.remote_id, result.url, at=now)
        log.info("%s done: %s", label, result.url or result.remote_id)
        outcomes.append(Outcome(post.id, target, True, result.url or result.remote_id))

    if not dry_run:
        notify(outcomes)
    return outcomes


def notify(outcomes: list[Outcome]) -> None:
    """Optional: send a summary to a Slack/Discord/Teams webhook (NOTIFY_WEBHOOK_URL)."""
    url = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not url or not outcomes:
        return
    lines = [f"{'✅' if o.ok else '❌'} {o.post_id} → {o.target}: {o.detail}" for o in outcomes]
    text = "socialq run\n" + "\n".join(lines)
    try:
        # "text" is read by Slack/Teams, "content" by Discord.
        requests.post(url, json={"text": text, "content": text[:2000]}, timeout=15)
    except requests.RequestException as exc:
        log.warning("Couldn't send notification: %s", exc)
