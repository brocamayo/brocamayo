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
