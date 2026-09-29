"""Command line: python -m socialq <command>"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import PLATFORMS, load_config
from .platforms import PublishError
from .queue import DATETIME_FORMAT, append_to_queue, load_queue, parse_datetime, validate
from .scheduler import run_once
from .slots import next_free_slot
from .state import AlreadyRunning, State, run_lock

log = logging.getLogger("socialq")
EXAMPLES_DIR = Path(__file__).resolve().parent.parent


def setup_logging(base_dir: Path, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:  # None under pythonw (the hidden scheduled task)
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        handlers.append(console)
    if base_dir.exists():
        file_handler = logging.FileHandler(base_dir / "socialq.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)
    log.handlers = handlers
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


# --- commands -----------------------------------------------------------------

def cmd_init(args) -> int:
    target = Path(args.dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    for name in ("config.example.yaml", "queue.example.yaml", ".env.example"):
        dest = target / name.replace(".example", "")
        if dest.exists():
            print(f"  exists, left alone: {dest.name}")
        elif name == "queue.example.yaml":
            # Start with an empty queue; keep the how-to comments from the example.
            header = [l for l in (EXAMPLES_DIR / name).read_text().splitlines() if l.startswith("#")]
            dest.write_text("\n".join(header) + "\n")
            print(f"  created {dest.name} (empty - see queue.example.yaml for a full example)")
        else:
            shutil.copy(EXAMPLES_DIR / name, dest)
            print(f"  created {dest.name}")
    for sub in ("videos", "credentials"):
        (target / sub).mkdir(exist_ok=True)
    print(f"\nNext: fill in {target / '.env'} and {target / 'config.yaml'}, then run "
          "`python -m socialq auth <platform>` for each platform.")
    return 0


def cmd_add(args) -> int:
    config = load_config(args.config)
    posts = load_queue(config)
    video = Path(args.video)
    if args.at:
        when = parse_datetime(args.at, config.timezone)
    else:
        when = next_free_slot(config.slots, [p.publish_at for p in posts],
                              datetime.now(config.timezone), config.timezone)
    # Store paths relative to media_dir when the video lives there.
    try:
        video_ref = str(video.resolve().relative_to(config.media_dir.resolve()))
    except ValueError:
        video_ref = str(video.resolve()) if video.exists() else args.video
    entry = {
        "id": args.id or f"{when:%Y%m%d-%H%M}-{Path(args.video).stem}".lower().replace(" ", "-"),
        "video": video_ref,
        "publish_at": when.strftime(DATETIME_FORMAT),
        "platforms": args.platforms or [p for p in PLATFORMS if config.enabled(p)],
        "title": args.title or Path(args.video).stem.replace("-", " ").replace("_", " "),
        "caption": args.caption or "",
    }
    if args.tags:
        entry["tags"] = [t.strip().lstrip("#") for t in args.tags.split(",") if t.strip()]
    if any(p.id == entry["id"] for p in posts):
        print(f"A post with id {entry['id']!r} already exists; pass --id", file=sys.stderr)
        return 1
    append_to_queue(config, entry)
    print(f"Queued {entry['id']} for {entry['publish_at']} on {', '.join(entry['platforms'])}")
    if when <= datetime.now(config.timezone):
        print("  note: that time has passed, so it will post on the next run")
    added = next(p for p in load_queue(config) if p.id == entry["id"])
    for problem in validate(added, config):
        print(f"  warning: {problem}")
    return 0


def cmd_list(args) -> int:
    config = load_config(args.config)
    state = State(config.state_file)
    posts = load_queue(config)
    if not posts:
        print("Queue is empty. Add one with: python -m socialq add videos/my-video.mp4")
        return 0
    now = datetime.now(config.timezone)
    for post in posts:
        if not args.all and all(state.is_published(post.id, p) for p in post.platforms):
            continue
        when = post.publish_at.astimezone(config.timezone).strftime("%a %b %d %H:%M")
        marker = "due " if post.publish_at <= now else "    "
        print(f"{marker}{when}  {post.id}")
        for platform in post.platforms:
            entry = state.get(post.id, platform)
            status = entry.get("status", "pending")
            if status == "failed":
                status += f" ({entry.get('attempts')}/{config.max_attempts}): {entry.get('last_error', '')[:100]}"
            elif status == "published":
                status += f"  {entry.get('url') or entry.get('remote_id')}"
            print(f"        {platform:<10} {status}")
    return 0


def cmd_check(args) -> int:
    config = load_config(args.config)
    posts = load_queue(config)
    bad = 0
    for post in posts:
        problems = validate(post, config)
        if problems:
            bad += 1
            print(f"FAIL  {post.id}")
            for p in problems:
                print(f"        - {p}")
        else:
            print(f"ok    {post.id}")
    print(f"\n{len(posts)} post(s), {bad} with problems")
    return 1 if bad else 0


def cmd_run(args) -> int:
    config = load_config(args.config)
    setup_logging(config.base_dir, args.verbose)
    try:
        with run_lock(config.base_dir / ".socialq.lock"):
            outcomes = run_once(config, dry_run=args.dry_run)
    except AlreadyRunning as exc:
        log.warning("%s", exc)
        return 0
    return 1 if any(not o.ok for o in outcomes) else 0


def cmd_watch(args) -> int:
    config = load_config(args.config)
    setup_logging(config.base_dir, args.verbose)
    log.info("Watching the queue every %d minute(s). Ctrl+C to stop.", args.every)
    while True:
        try:
            with run_lock(config.base_dir / ".socialq.lock"):
                run_once(load_config(args.config))
        except AlreadyRunning as exc:
            log.warning("%s", exc)
        except Exception:  # never let the watcher die on one bad run
            log.exception("Run failed")
        time.sleep(args.every * 60)


def cmd_retry(args) -> int:
    config = load_config(args.config)
    state = State(config.state_file)
    state.reset(args.post_id, args.platform)
    print(f"Reset {args.post_id}{' -> ' + args.platform if args.platform else ''}; "
          "it will be retried on the next run.")
    return 0


def cmd_auth(args) -> int:
    config = load_config(args.config)
    try:
        if args.platform == "youtube":
            from .platforms.youtube import authorize
            authorize(config)
            print("YouTube authorized.")
        elif args.platform == "tiktok":
            from .platforms.tiktok import authorize
            authorize(config)
        else:
            from .platforms.instagram import authorize
            authorize(config, args.token, refresh=args.refresh)
    except PublishError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


# --- parser -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="socialq", description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create config.yaml, queue.yaml and .env in a folder")
    p.add_argument("dir", nargs="?", default=".")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="queue a video (auto-picks the next free slot)")
    p.add_argument("video")
    p.add_argument("-t", "--title")
    p.add_argument("--caption", help="caption / description (tags are appended as #hashtags)")
    p.add_argument("--tags", help="comma separated, e.g. investing,stocks")
    p.add_argument("--at", help="'YYYY-MM-DD HH:MM' in your timezone; default = next free slot")
    p.add_argument("-p", "--platforms", nargs="+", choices=PLATFORMS)
    p.add_argument("--id")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="show upcoming posts and their status")
    p.add_argument("-a", "--all", action="store_true", help="include fully published posts")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("check", help="validate the queue (files exist, caption limits)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="publish everything that's due, then exit (for cron/Task Scheduler)")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("watch", help="keep running, checking the queue every few minutes")
    p.add_argument("--every", type=int, default=5, help="minutes between checks")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("retry", help="clear a failed/finished status so it posts again")
    p.add_argument("post_id")
    p.add_argument("platform", nargs="?", choices=PLATFORMS)
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("auth", help="log in to a platform")
    p.add_argument("platform", choices=PLATFORMS)
    p.add_argument("--token", help="instagram: short-lived token from Graph API Explorer")
    p.add_argument("--refresh", action="store_true", help="instagram: extend the saved token")
    p.set_defaults(func=cmd_auth)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
