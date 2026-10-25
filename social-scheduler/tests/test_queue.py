from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from socialq.queue import append_to_queue, load_queue, parse_datetime, validate

LA = ZoneInfo("America/Los_Angeles")


def test_parse_datetime_localizes_naive_times():
    dt = parse_datetime("2026-10-05 17:00", LA)
    assert dt == datetime(2026, 10, 5, 17, 0, tzinfo=LA)


def test_parse_datetime_keeps_explicit_offset():
    dt = parse_datetime("2026-10-05T17:00:00+00:00", LA)
    assert dt.utcoffset().total_seconds() == 0


def test_parse_datetime_rejects_garbage():
    with pytest.raises(ValueError):
        parse_datetime("next tuesday", LA)


def test_load_queue_defaults_and_overrides(config, project):
    (project / "queue.yaml").write_text(
        """
- video: My Video.mp4
  publish_at: "2026-10-05 17:00"
  title: Shared title
  caption: Shared caption
  tags: [investing, "#stocks"]
  tiktok:
    caption: Custom tiktok caption
"""
    )
    [post] = load_queue(config)
    assert post.id == "my-video"
    assert [str(t) for t in post.targets(config)] == [
        "main/youtube", "main/instagram", "main/tiktok", "main/x", "second/youtube", "second/tiktok"]
    assert post.social_caption("instagram") == "Shared caption\n\n#investing #stocks"
    assert post.social_caption("tiktok") == "Custom tiktok caption"
    assert post.youtube_tags() == ["investing", "stocks"]
    assert post.youtube_description() == "Shared caption"


def test_duplicate_ids_rejected(config, project):
    (project / "queue.yaml").write_text(
        "- {id: a, video: a.mp4, publish_at: '2026-10-05 17:00'}\n"
        "- {id: a, video: b.mp4, publish_at: '2026-10-06 17:00'}\n"
    )
    with pytest.raises(ValueError, match="Duplicate"):
        load_queue(config)


def test_append_keeps_comments_and_existing_entries(config, project):
    (project / "queue.yaml").write_text("# my notes\n")
    append_to_queue(config, {"id": "a", "video": "a.mp4", "publish_at": "2026-10-05 17:00"})
    append_to_queue(config, {"id": "b", "video": "b.mp4", "publish_at": "2026-10-06 17:00"})
    text = (project / "queue.yaml").read_text()
    assert text.startswith("# my notes")
    assert [p.id for p in load_queue(config)] == ["a", "b"]


def test_validate_flags_missing_file_and_long_title(config, project):
    (project / "queue.yaml").write_text(
        f"- {{video: missing.mp4, publish_at: '2026-10-05 17:00', title: '{'x' * 101}'}}\n"
    )
    [post] = load_queue(config)
    problems = validate(post, config)
    assert any("not found" in p for p in problems)
    assert any("youtube title" in p for p in problems)


def test_validate_flags_unknown_account(config, project):
    (project / "videos" / "a.mp4").write_bytes(b"v")
    (project / "queue.yaml").write_text("- {video: a.mp4, publish_at: '2026-10-05 17:00', title: T, accounts: [nope]}\n")
    [post] = load_queue(config)
    problems = validate(post, config)
    assert any("unknown account 'nope'" in p for p in problems)


def test_single_account_config_still_works(project):
    from socialq.config import load_config

    (project / "config.yaml").write_text("youtube: {enabled: true}\ntiktok: {enabled: false}\n")
    config = load_config(project / "config.yaml")
    assert list(config.accounts) == ["main"]
    assert config.accounts["main"].platforms == ["youtube"]
    assert config.settings("main", "youtube")["token_file"] == "credentials/main/youtube_token.json"


def test_account_settings_override_shared_ones(config):
    assert config.settings("main", "tiktok")["mode"] == "direct"
    assert config.settings("second", "tiktok")["mode"] == "inbox"
    assert config.settings("second", "tiktok")["chunk_size_mb"] == 5
    assert config.settings("second", "tiktok")["token_file"] == "credentials/second/tiktok_token.json"


def test_social_caption_drops_shorts_and_keeps_own_hashtags():
    from socialq.queue import Post

    post = Post(id="p", video="v.mp4", publish_at=datetime(2026, 10, 5, tzinfo=LA),
                caption="I Built an App #Shorts. Type what you owe.\n\nNot advice.\n\n#Shorts #debtfree #budgeting",
                tags=["debt-snowball", "shorts"])
    expected = "I Built an App. Type what you owe.\n\nNot advice.\n\n#debtfree #budgeting"
    assert post.social_caption("instagram") == expected
    assert post.social_caption("tiktok") == expected
    assert post.youtube_description().endswith("#Shorts #debtfree #budgeting")


def test_tag_hashtags_drop_dashes_and_youtube_only_tags():
    from socialq.queue import Post

    post = Post(id="p", video="v.mp4", publish_at=datetime(2026, 10, 5, tzinfo=LA),
                caption="Plain caption", tags=["debt-snowball", "credit card debt", "Shorts"])
    assert post.social_caption("instagram") == "Plain caption\n\n#debtsnowball #creditcarddebt"


def test_add_command_per_platform_captions(project):
    from socialq.cli import main
    from socialq.config import load_config

    (project / "videos" / "v.mp4").write_bytes(b"x")
    main(["-c", str(project / "config.yaml"), "add", str(project / "videos" / "v.mp4"), "--title", "T",
          "--caption", "Everywhere", "--ig-caption", "Link in bio", "--tiktok-caption", "For TikTok",
          "--at", "2026-10-01 09:00"])
    [post] = load_queue(load_config(project / "config.yaml"))
    assert post.youtube_description() == "Everywhere"
    assert post.social_caption("instagram") == "Link in bio"
    assert post.social_caption("tiktok") == "For TikTok"
    assert post.social_caption("x") == "Everywhere"


def test_retry_failed_resets_only_failed_uploads(project, capsys):
    from socialq.cli import main
    from socialq.config import load_config
    from socialq.state import State

    state = State(load_config(project / "config.yaml").state_file)
    state.record_failure("a", "main/instagram", "boom")
    state.record_failure("b", "main/tiktok", "boom")
    state.record_success("b", "main/instagram", "m1", None)
    state.record_waiting("c", "main/instagram", "log in")

    assert main(["-c", str(project / "config.yaml"), "retry", "failed", "instagram"]) == 0
    assert "a main/instagram" in capsys.readouterr().out
    state = State(load_config(project / "config.yaml").state_file)
    assert state.get("a", "main/instagram") == {}
    assert state.get("b", "main/tiktok")["status"] == "failed"
    assert state.is_published("b", "main/instagram")
    assert state.get("c", "main/instagram")["status"] == "waiting"


def test_remove_takes_one_post_out_and_keeps_the_rest(project, capsys):
    from socialq.cli import main
    from socialq.config import load_config
    from socialq.queue import load_queue
    from socialq.state import State

    (project / "videos" / "v.mp4").write_bytes(b"x")
    cfg = str(project / "config.yaml")
    (project / "queue.yaml").write_text("# my queue\n")
    for pid in ("first", "middle", "last"):
        assert main(["-c", cfg, "add", str(project / "videos" / "v.mp4"), "--id", pid, "--title", "T",
                     "--caption", "line one\n\nline two #Shorts", "--at", "2026-11-01 09:00"]) == 0
    State(load_config(project / "config.yaml").state_file).record_success("middle", "main/youtube", "y1", None)

    assert main(["-c", cfg, "remove", "middle"]) == 0
    out = capsys.readouterr().out
    assert "already posted (left as is): main/youtube" in out
    config = load_config(project / "config.yaml")
    assert [p.id for p in load_queue(config)] == ["first", "last"]
    assert all(p.caption == "line one\n\nline two #Shorts" for p in load_queue(config))
    assert (project / "queue.yaml").read_text().startswith("# my queue\n")

    assert main(["-c", cfg, "remove", "last"]) == 0
    assert main(["-c", cfg, "remove", "nope"]) == 1
    assert [p.id for p in load_queue(load_config(project / "config.yaml"))] == ["first"]


def test_add_copies_outside_videos_and_run_deletes_the_copy_once_posted(project, tmp_path_factory, capsys):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from socialq.cli import main
    from socialq.config import load_config
    from socialq.platforms import PublishResult
    from socialq.queue import load_queue
    from socialq.scheduler import run_once

    desktop = tmp_path_factory.mktemp("Desktop")
    (desktop / "short.mp4").write_bytes(b"video-bytes")
    cfg = str(project / "config.yaml")
    for pid, platforms in (("yt", ["youtube"]), ("social", ["tiktok"])):
        assert main(["-c", cfg, "add", str(desktop / "short.mp4"), "--id", pid, "--title", "T",
                     "--accounts", "main", "--platforms", *platforms, "--at", "2026-10-05 17:00"]) == 0
    config = load_config(project / "config.yaml")
    posts = load_queue(config)
    assert [p.video for p in posts] == ["queued/short.mp4", "queued/short.mp4"]   # one shared copy
    copy = project / "videos" / "queued" / "short.mp4"
    assert copy.read_bytes() == b"video-bytes"
    (desktop / "short.mp4").unlink()   # moving the original no longer matters

    class Ok:
        def __init__(self, ok):
            self.ok = ok

        def publish(self, post, video):
            assert video.read_bytes() == b"video-bytes"
            if not self.ok:
                from socialq.platforms import TryLater
                raise TryLater("slow")
            return PublishResult("id")

    due = datetime(2026, 10, 5, 17, 5, tzinfo=ZoneInfo("America/Los_Angeles"))
    run_once(config, now=due, publisher_factory=lambda t, c: Ok(t.platform == "youtube"))
    assert copy.exists()   # tiktok still waiting on it
    run_once(config, now=due.replace(hour=18), publisher_factory=lambda t, c: Ok(True))
    assert not copy.exists()


def test_copy_videos_moves_existing_queue_entries_into_the_videos_folder(project, tmp_path_factory):
    from socialq.cli import main
    from socialq.config import load_config
    from socialq.queue import load_queue

    desktop = tmp_path_factory.mktemp("Desk top")
    (desktop / "a b.mp4").write_bytes(b"aaa")
    (project / "queue.yaml").write_text(
        "# keep me\n"
        f"- id: one\n  video: {desktop / 'a b.mp4'}\n  publish_at: '2026-10-05 17:00'\n  title: One\n\n"
        f"- id: two\n  video: '{desktop / 'a b.mp4'}'\n  publish_at: '2026-10-05 18:00'\n  title: Two\n\n"
        f"- id: gone\n  video: {desktop / 'missing.mp4'}\n  publish_at: '2026-10-05 19:00'\n  title: Gone\n")
    cfg = str(project / "config.yaml")
    assert main(["-c", cfg, "copy-videos"]) == 0
    posts = {p.id: p.video for p in load_queue(load_config(project / "config.yaml"))}
    assert posts["one"] == posts["two"] == "queued/a b.mp4"
    assert posts["gone"] == str(desktop / "missing.mp4")
    assert (project / "queue.yaml").read_text().startswith("# keep me\n")
    assert main(["-c", cfg, "copy-videos"]) == 0   # second run: nothing to do
