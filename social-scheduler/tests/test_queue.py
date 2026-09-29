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
    assert post.platforms == ["youtube", "instagram", "tiktok"]
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
