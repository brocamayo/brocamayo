"""Pick up finished videos and hand each new packaging file to Claude Code to check and queue.

A "package" is a `NAME-packaging.md` file (title, description, captions...) next to the video
files it describes (`NAME.mp4`, `NAME.srt`; one packaging file can cover several shorts).
`./sq intake` (run by launchd every few minutes) looks for new or changed packaging files in
the intake folders, runs `claude -p` in this folder for each one (it follows CLAUDE.md, writes
a spec and runs `./sq add-spec`), records the result in intake.json and sends the summary to
Broc's phone. The regular `./sq run` job then posts whatever is due.

Claude ends its answer with one status line:
  STATUS: queued     everything in the package is queued (done)
  STATUS: deferred   something is waiting (a video file, or the long video a short links to);
                     try again in retry_minutes, for up to give_up_hours
  STATUS: needs-you  a check failed; Broc has to decide (done, until the file changes)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import notify

log = logging.getLogger("socialq")

STATE_FILE = "intake.json"
PARTIAL_SUFFIXES = {".crdownload", ".download", ".part", ".partial"}
MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v", ".srt", ".vtt", ".png", ".jpg", ".jpeg"}
DONE = ("queued", "needs-you")
STATUS_RE = re.compile(r"^\s*STATUS:\s*(queued|deferred|needs-you)\b", re.I | re.M)

DEFAULTS = {
    "folders": ["~/Downloads", "inbox"],
    "command": "claude",
    "settle_seconds": 120,      # wait until the files stop changing (still downloading/exporting)
    "retry_minutes": 30,
    "give_up_hours": 48,
    "max_per_run": 3,
    "timeout_minutes": 30,
    "max_turns": 80,
    "ignore_older_than_days": 3,  # don't pick up old packaging files the first time intake runs
}

ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch",
    "Bash(./sq:*)", "Bash(ls:*)", "Bash(find:*)",
]

PROMPT = """A new video package arrived for socialq. You are running unattended on Broc's Mac:
nobody can answer questions, so don't ask any. Follow CLAUDE.md in this folder.

Packaging file: {packaging}
Video, caption and image files in that folder from the last week:
{files}

1. Read the packaging file. It may cover one video or several (e.g. several shorts).
2. Run `./sq list --all` and skip any video that is already queued (ids come from the video file
   name, see CLAUDE.md).
3. For each remaining video, run every check in CLAUDE.md. If all pass, write a spec to
   intake/specs/<id>.json and run `./sq add-spec intake/specs/<id>.json` (then
   `./sq spread instagram facebook` if it queued Instagram/Facebook posts). Don't run `./sq run`:
   the background job posts within 10 minutes.
4. If a video file isn't there yet, or a short's long video isn't posted yet (`./sq url <long-id>`
   prints nothing), leave that video for later.
5. If a check fails and you can't fix it the way CLAUDE.md allows, don't queue that video.

Then write a short summary for Broc's phone, plain text, under 1,200 characters:
what you queued and when it posts, what you skipped or left for later and why, and his manual
steps (pin comments, TikTok drafts, thumbnails, end screens).
End with exactly one line, the last line of your answer:
STATUS: queued      (every video in the package is queued, or was already)
STATUS: deferred    (something is still waiting on a file or a long video)
STATUS: needs-you   (a check failed and Broc must decide)
"""


@dataclass
class Result:
    status: str          # queued | deferred | needs-you | error
    summary: str


def settings(config) -> dict:
    merged = dict(DEFAULTS)
    merged.update(config.intake or {})
    return merged


def folders(config, opts: dict) -> list[Path]:
    out = []
    for f in opts["folders"]:
        p = Path(os.path.expanduser(str(f)))
        out.append(p if p.is_absolute() else config.base_dir / p)
    return out


def load_state(config) -> dict:
    path = config.base_dir / STATE_FILE
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (ValueError, OSError):
        return {}


def save_state(config, state: dict) -> None:
    path = config.base_dir / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _settled(folder: Path, packaging: Path, now: float, settle: int) -> bool:
    """False while the packaging file, or any video/caption file or unfinished download in the
    folder, changed in the last `settle` seconds (a video may still be exporting or downloading)."""
    for f in folder.iterdir():
        suffix = f.suffix.lower()
        if f == packaging or suffix in MEDIA_SUFFIXES or suffix in PARTIAL_SUFFIXES:
            try:
                if now - f.stat().st_mtime < settle:
                    return False
            except OSError:
                continue
    return True


def _packagings(folder: Path) -> list[Path]:
    """Packaging files in a folder. os.listdir (unlike glob) raises PermissionError when macOS blocks
    the folder, so a missing Full Disk Access shows up instead of looking like an empty folder."""
    names = os.listdir(folder)
    return [folder / n for n in names if n.endswith("-packaging.md") and not n.startswith(".")]


def due_packages(config, opts: dict, state: dict, now: float) -> list[Path]:
    """Packaging files that are new, changed, or waiting for a retry."""
    due = []
    for folder in folders(config, opts):
        if not folder.is_dir():
            continue
        found = sorted(_packagings(folder), key=lambda p: p.stat().st_mtime)
        for pkg in found:
            try:
                mtime = pkg.stat().st_mtime
            except OSError:
                continue
            entry = state.get(str(pkg))
            if entry is None:
                if now - mtime > opts["ignore_older_than_days"] * 86400:
                    continue
            elif entry.get("mtime") == mtime:
                if entry.get("status") in DONE:
                    continue
                if now - entry.get("first_try", now) > opts["give_up_hours"] * 3600:
                    continue
                if now - entry.get("last_try", 0) < opts["retry_minutes"] * 60:
                    continue
            if not _settled(folder, pkg, now, opts["settle_seconds"]):
                continue
            due.append(pkg)
    return due[: opts["max_per_run"]]


def recent_files(folder: Path, now: float) -> list[str]:
    files = []
    for f in folder.iterdir():
        try:
            if f.suffix.lower() in MEDIA_SUFFIXES and now - f.stat().st_mtime < 7 * 86400:
                files.append(f"- {f}")
        except OSError:
            continue
    return sorted(files) or ["- (none)"]


def find_claude(command: str) -> str | None:
    if os.path.sep in command:
        return command if os.access(command, os.X_OK) else None
    extra = [Path.home() / ".local/bin", Path.home() / ".claude/local", Path("/opt/homebrew/bin"),
             Path("/usr/local/bin")]
    path = os.pathsep.join([os.environ.get("PATH", ""), *map(str, extra)])
    return shutil.which(command, path=path)


def parse_status(output: str) -> str:
    found = STATUS_RE.findall(output or "")
    return found[-1].lower() if found else "error"


def handle(config, opts: dict, pkg: Path, now: float) -> Result:
    claude = find_claude(str(opts["command"]))
    if not claude:
        return Result("error", "Claude Code isn't installed (or isn't on the PATH), so I can't check "
                               "and queue this package. Install it, or set intake.command in config.yaml.")
    (config.base_dir / "intake" / "specs").mkdir(parents=True, exist_ok=True)
    prompt = PROMPT.format(packaging=pkg, files="\n".join(recent_files(pkg.parent, now)))
    cmd = [claude, "-p", prompt, "--permission-mode", "acceptEdits",
           "--max-turns", str(opts["max_turns"]), "--allowedTools", *ALLOWED_TOOLS]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(claude).parent), env.get("PATH", "/usr/bin:/bin")])
    log.info("intake: handing %s to Claude", pkg.name)
    try:
        done = subprocess.run(cmd, cwd=config.base_dir, env=env, capture_output=True, text=True,
                              timeout=opts["timeout_minutes"] * 60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Result("deferred", f"Claude took longer than {opts['timeout_minutes']} min; I'll try again.")
    except OSError as exc:
        return Result("error", f"Couldn't start Claude Code: {exc}")
    output = (done.stdout or "").strip()
    if done.returncode != 0 and not output:
        return Result("deferred", f"Claude Code failed ({(done.stderr or '').strip()[-300:]}); I'll try again.")
    status = parse_status(output)
    summary = STATUS_RE.sub("", output).strip() or "(no summary)"
    return Result(status, summary)


def mark_seen(config) -> int:
    """Record every packaging file that's there now as done, so intake only handles new ones."""
    opts = settings(config)
    state = load_state(config)
    count = 0
    for folder in folders(config, opts):
        if not folder.is_dir():
            continue
        for pkg in _packagings(folder):
            entry = state.get(str(pkg), {})
            entry.update(mtime=pkg.stat().st_mtime, status="queued", summary="(already handled before intake)")
            state[str(pkg)] = entry
            count += 1
    save_state(config, state)
    return count


def run_intake(config, now: float | None = None, handler=handle) -> list[tuple[Path, Result]]:
    opts = settings(config)
    now = time.time() if now is None else now
    state = load_state(config)
    try:
        due = due_packages(config, opts, state, now)
    except PermissionError as exc:
        warned = state.get("_access_warned", 0)
        if now - warned > 86400:
            import sys
            notify.push("socialq can't read your Downloads",
                        "macOS is blocking the background job. Give Full Disk Access to:\n"
                        f"{os.path.realpath(sys.executable)}\n(System Settings → Privacy & Security → "
                        "Full Disk Access → +, then ⌘⇧G and paste the path.)", tags="warning")
            state["_access_warned"] = now
            save_state(config, state)
        log.warning("intake: %s", exc)
        return []
    results = []
    for pkg in due:
        mtime = pkg.stat().st_mtime
        entry = state.get(str(pkg))
        if entry is None or entry.get("mtime") != mtime:
            entry = {"mtime": mtime, "first_try": now, "tries": 0}
        result = handler(config, opts, pkg, now)
        first_deferral = result.status == "deferred" and entry.get("status") != "deferred"
        entry.update(status=result.status, last_try=now, tries=entry.get("tries", 0) + 1,
                     summary=result.summary[-2000:],
                     at=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"))
        if result.status == "error":
            # Retry like a deferral, but don't spin: the retry clock applies.
            entry["status"] = "deferred"
        state[str(pkg)] = entry
        save_state(config, state)
        log.info("intake: %s -> %s", pkg.name, result.status)
        if result.status != "deferred" or first_deferral:
            title = {"queued": "✅ Queued", "deferred": "⏳ Waiting", "needs-you": "✋ Needs you",
                     "error": "⚠️ Problem"}[result.status]
            notify.push(f"{title}: {pkg.name[: -len('-packaging.md')]}", result.summary,
                        tags={"queued": "white_check_mark", "needs-you": "raised_hand"}.get(result.status, ""))
        results.append((pkg, result))
    return results
