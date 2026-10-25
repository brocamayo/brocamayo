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
from .platforms import NotLoggedIn, PublishError, Publisher, TryLater, get_publisher
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
    posts = load_queue(config)
    work = due_work(posts, state, config, now)
    if not work:
        log.info("Nothing due.")

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
        except TryLater as exc:
            since = state.get(post.id, target.key).get("try_later_since") or now.isoformat()
            if datetime.fromisoformat(since) + timedelta(hours=config.try_later_hours) <= now:
                msg = f"{exc} (kept failing for {config.try_later_hours}h)"
                state.record_failure(post.id, target.key, msg, at=now)
                log.error("%s failed: %s", label, msg)
            else:
                state.record_waiting(post.id, target.key, str(exc), at=now)
                state.update(post.id, target.key, try_later_since=since)
                log.warning("%s will try again in %d min: %s", label, config.retry_delay_minutes, exc)
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
        post_comments(config, state, posts, now)
    return outcomes


def post_comments(config: Config, state: State, posts: list[Post], now: datetime) -> None:
    """Queued YouTube comments; never lets a problem here fail the run."""
    try:
        from .platforms.youtube import post_pending_comments
        post_pending_comments(config, state, posts, now)
    except Exception:
        log.error("Posting YouTube comments crashed:\n%s", traceback.format_exc())


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
