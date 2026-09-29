from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from socialq.platforms import PublishError, PublishResult
from socialq.scheduler import run_once
from socialq.state import State

LA = ZoneInfo("America/Los_Angeles")


class StubPublisher:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, 0

    def publish(self, post, video):
        self.calls += 1
        if self.fail:
            raise PublishError(f"{self.name} is down")
        return PublishResult(remote_id=f"{self.name}-{post.id}", url=f"https://{self.name}/{post.id}")


def make_queue(project):
    (project / "videos" / "a.mp4").write_bytes(b"video")
    (project / "videos" / "b.mp4").write_bytes(b"video")
    (project / "queue.yaml").write_text(
        """
- {id: due, video: a.mp4, publish_at: "2026-10-05 17:00", title: Due}
- {id: later, video: b.mp4, publish_at: "2026-10-09 17:00", title: Later}
"""
    )


def test_publishes_only_due_posts_and_never_twice(config, project):
    make_queue(project)
    pubs = {n: StubPublisher(n) for n in ("youtube", "instagram", "tiktok")}
    now = datetime(2026, 10, 5, 17, 1, tzinfo=LA)

    outcomes = run_once(config, now=now, publisher_factory=lambda n, c: pubs[n])
    assert sorted(o.platform for o in outcomes) == ["instagram", "tiktok", "youtube"]
    assert all(o.post_id == "due" and o.ok for o in outcomes)

    assert run_once(config, now=now, publisher_factory=lambda n, c: pubs[n]) == []
    assert all(p.calls == 1 for p in pubs.values())
    assert State(config.state_file).get("due", "youtube")["url"] == "https://youtube/due"


def test_one_platform_failing_does_not_block_others_and_stops_after_max_attempts(config, project):
    make_queue(project)
    pubs = {"youtube": StubPublisher("youtube"), "instagram": StubPublisher("instagram", fail=True),
            "tiktok": StubPublisher("tiktok")}
    now = datetime(2026, 10, 5, 17, 1, tzinfo=LA)
    factory = lambda n, c: pubs[n]  # noqa: E731

    first = run_once(config, now=now, publisher_factory=factory)
    assert {o.platform: o.ok for o in first} == {"youtube": True, "instagram": False, "tiktok": True}

    assert run_once(config, now=now, publisher_factory=factory) == []  # waiting out retry delay

    later = now + timedelta(minutes=config.retry_delay_minutes + 1)
    second = run_once(config, now=later, publisher_factory=factory)  # retries only instagram
    assert [(o.platform, o.ok) for o in second] == [("instagram", False)]

    much_later = later + timedelta(days=1)
    assert run_once(config, now=much_later, publisher_factory=factory) == []  # max_attempts = 2
    assert pubs["instagram"].calls == 2

    State(config.state_file).reset("due", "instagram")
    pubs["instagram"].fail = False
    third = run_once(config, now=much_later, publisher_factory=factory)
    assert [(o.platform, o.ok) for o in third] == [("instagram", True)]


def test_dry_run_publishes_nothing(config, project):
    make_queue(project)
    now = datetime(2026, 10, 5, 17, 1, tzinfo=LA)

    def boom(name, cfg):
        raise AssertionError("dry run must not create publishers")

    outcomes = run_once(config, now=now, dry_run=True, publisher_factory=boom)
    assert len(outcomes) == 3
    assert not config.state_file.exists()


def test_missing_video_is_reported_not_crashed(config, project):
    (project / "queue.yaml").write_text("- {id: x, video: nope.mp4, publish_at: '2026-10-05 17:00', title: X}\n")
    now = datetime(2026, 10, 5, 17, 1, tzinfo=LA)
    outcomes = run_once(config, now=now, publisher_factory=lambda n, c: StubPublisher(n))
    assert outcomes and not any(o.ok for o in outcomes)
    assert "not found" in outcomes[0].detail
