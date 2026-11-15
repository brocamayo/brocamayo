import json
import os
import time

import pytest

from socialq import cli, intake
from socialq.config import load_config
from socialq.queue import load_queue
from socialq.state import State


def write_video(path):
    path.write_bytes(b"\x00\x00\x00\x08free")
    return path


# --- add-spec / url / reschedule --------------------------------------------------

def test_spec_to_argv_maps_every_field(tmp_path):
    argv = cli.spec_to_argv({
        "video": "~/Downloads/a.mp4", "id": "a", "title": "T $5", "caption": "C",
        "captions": "~/Downloads/a.srt", "comment": "pin me", "at": "2026-10-01 09:00",
        "tiktok_caption": "tt", "accounts": ["brocmayo"], "platforms": ["youtube", "tiktok"],
        "translations": {"es": {"title": "Título", "description": "Desc"}}, "tags": ["a", "b"],
    })
    assert argv[0] == "add" and argv[1].endswith("/Downloads/a.mp4")
    assert ["--title", "T $5"] == argv[argv.index("--title"):argv.index("--title") + 2]
    assert argv[argv.index("--accounts") + 1] == "brocmayo"
    assert argv[argv.index("--platforms") + 1:argv.index("--platforms") + 3] == ["youtube", "tiktok"]
    i = argv.index("--translation")
    assert argv[i + 1:i + 4] == ["es", "Título", "Desc"]
    assert argv[argv.index("--tags") + 1] == "a,b"


def test_spec_rejects_unknown_fields():
    with pytest.raises(ValueError, match="unknown spec field"):
        cli.spec_to_argv({"video": "a.mp4", "titel": "typo"})


def test_add_spec_queues_posts(project):
    video = write_video(project / "videos" / "short.mp4")
    spec = project / "spec.json"
    spec.write_text(json.dumps([
        {"video": str(video), "id": "short", "title": "Hi", "caption": "C", "accounts": ["main"],
         "platforms": ["youtube"], "at": "2026-10-01 09:00", "comment": "pin"},
        {"video": str(video), "id": "short-social", "title": "Hi", "caption": "C", "accounts": ["main"],
         "platforms": ["tiktok"], "tiktok_caption": "tt", "at": "2026-10-01 09:00"},
    ]))
    assert cli.main(["--config", str(project / "config.yaml"), "add-spec", str(spec)]) == 0
    posts = {p.id: p for p in load_queue(load_config(project / "config.yaml"))}
    assert set(posts) == {"short", "short-social"}
    assert posts["short"].option("youtube", "comment") == "pin"
    # A second run is refused per id instead of duplicating.
    assert cli.main(["--config", str(project / "config.yaml"), "add-spec", str(spec)]) == 1
    assert len(load_queue(load_config(project / "config.yaml"))) == 2


def test_url_prints_published_youtube_link(project, capsys):
    config = load_config(project / "config.yaml")
    state = State(config.state_file)
    state.record_success("long", "main/tiktok", "t1", "https://tiktok/x")
    state.record_success("long", "main/youtube", "y1", "https://youtu.be/y1")
    assert cli.main(["--config", str(project / "config.yaml"), "url", "long"]) == 0
    assert capsys.readouterr().out.strip() == "https://youtu.be/y1"
    assert cli.main(["--config", str(project / "config.yaml"), "url", "nope"]) == 1
    assert capsys.readouterr().out == ""


def test_reschedule_now_keeps_order(project):
    video = write_video(project / "videos" / "v.mp4")
    cfg = str(project / "config.yaml")
    for pid in ("one", "two"):
        assert cli.main(["--config", cfg, "add", str(video), "--id", pid, "--at", "2030-01-01 09:00"]) == 0
    assert cli.main(["--config", cfg, "reschedule", "one", "two", "--at", "now"]) == 0
    posts = {p.id: p for p in load_queue(load_config(cfg))}
    assert posts["one"].publish_at < posts["two"].publish_at
    from datetime import datetime
    assert posts["two"].publish_at <= datetime.now(posts["two"].publish_at.tzinfo)
    assert cli.main(["--config", cfg, "reschedule", "ghost", "--at", "now"]) == 1


# --- intake ---------------------------------------------------------------------------

@pytest.fixture
def inbox(project):
    folder = project / "inbox"
    folder.mkdir()
    return folder


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def make_package(inbox, name="Nvidia-Short", old=600):
    pkg = inbox / f"{name}-packaging.md"
    pkg.write_text("Title: x")
    vid = write_video(inbox / f"{name}.mp4")
    for f in (pkg, vid):
        age(f, old)
    return pkg


class FakeHandler:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = []

    def __call__(self, config, opts, pkg, now):
        self.calls.append(pkg.name)
        return intake.Result(self.statuses.pop(0), f"summary {len(self.calls)}")


def test_intake_handles_a_new_package_once(config, inbox, monkeypatch):
    pushed = []
    monkeypatch.setattr(intake.notify, "push", lambda title, body, tags="": pushed.append(title) or True)
    make_package(inbox)
    handler = FakeHandler("queued")
    intake.run_intake(config, handler=handler)
    intake.run_intake(config, handler=handler)
    assert handler.calls == ["Nvidia-Short-packaging.md"]
    assert pushed == ["✅ Queued: Nvidia-Short"]


def test_intake_waits_while_files_are_still_changing(config, inbox):
    pkg = make_package(inbox)
    (inbox / "Nvidia-Short-2.mp4.crdownload").write_bytes(b"...")
    handler = FakeHandler("queued")
    intake.run_intake(config, handler=handler)
    assert handler.calls == []
    age(inbox / "Nvidia-Short-2.mp4.crdownload", 600)
    intake.run_intake(config, handler=handler)
    assert handler.calls == [pkg.name]


def test_intake_retries_deferred_packages_and_notifies_once(config, inbox, monkeypatch):
    pushed = []
    monkeypatch.setattr(intake.notify, "push", lambda title, body, tags="": pushed.append(title) or True)
    make_package(inbox)
    handler = FakeHandler("deferred", "deferred", "queued")
    now = time.time()
    intake.run_intake(config, now=now, handler=handler)
    intake.run_intake(config, now=now + 60, handler=handler)          # too soon to retry
    assert len(handler.calls) == 1
    intake.run_intake(config, now=now + 31 * 60, handler=handler)
    intake.run_intake(config, now=now + 62 * 60, handler=handler)
    assert len(handler.calls) == 3
    assert pushed == ["⏳ Waiting: Nvidia-Short", "✅ Queued: Nvidia-Short"]


def test_intake_gives_up_after_the_limit(config, inbox):
    make_package(inbox)
    handler = FakeHandler(*["deferred"] * 5)
    now = time.time()
    intake.run_intake(config, now=now, handler=handler)
    intake.run_intake(config, now=now + 49 * 3600, handler=handler)
    assert len(handler.calls) == 1


def test_intake_rehandles_an_edited_package(config, inbox):
    pkg = make_package(inbox)
    handler = FakeHandler("needs-you", "queued")
    intake.run_intake(config, handler=handler)
    pkg.write_text("Title: fixed")
    age(pkg, 600)
    intake.run_intake(config, handler=handler)
    assert len(handler.calls) == 2


def test_intake_skips_old_files_and_mark_seen(config, inbox):
    make_package(inbox, "Old", old=10 * 86400)
    make_package(inbox, "Fresh")
    assert intake.mark_seen(config) == 2
    handler = FakeHandler("queued")
    intake.run_intake(config, handler=handler)
    assert handler.calls == []


def test_parse_status_uses_the_last_line():
    assert intake.parse_status("blah\nSTATUS: deferred\nmore\nSTATUS: queued") == "queued"
    assert intake.parse_status("STATUS: needs-you") == "needs-you"
    assert intake.parse_status("no status") == "error"


def test_handle_without_claude_reports_error(config, inbox, monkeypatch):
    monkeypatch.setattr(intake, "find_claude", lambda command: None)
    pkg = make_package(inbox)
    result = intake.handle(config, intake.settings(config), pkg, time.time())
    assert result.status == "error" and "isn't installed" in result.summary


def test_handle_runs_claude_and_reads_status(config, inbox, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 'Queued 1 short for 09:00.'\necho 'STATUS: queued'\n")
    fake.chmod(0o755)
    pkg = make_package(inbox)
    opts = intake.settings(config) | {"command": str(fake)}
    result = intake.handle(config, opts, pkg, time.time())
    assert result.status == "queued"
    assert result.summary == "Queued 1 short for 09:00."
    assert (config.base_dir / "intake" / "specs").is_dir()


def test_intake_warns_when_macos_blocks_the_folder(config, inbox, monkeypatch):
    pushed = []
    monkeypatch.setattr(intake.notify, "push", lambda title, body, tags="": pushed.append(body) or True)

    def blocked(path):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(intake.os, "listdir", blocked)
    handler = FakeHandler()
    now = time.time()
    assert intake.run_intake(config, now=now, handler=handler) == []
    intake.run_intake(config, now=now + 60, handler=handler)       # warns once a day, not every run
    assert len(pushed) == 1 and "Full Disk Access" in pushed[0]
