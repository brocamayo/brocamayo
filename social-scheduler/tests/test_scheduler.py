from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from socialq.platforms import PublishError, PublishResult
from socialq.scheduler import run_once
from socialq.state import State

LA = ZoneInfo("America/Los_Angeles")
DUE = datetime(2026, 10, 5, 17, 1, tzinfo=LA)


class StubPublisher:
    def __init__(self, key, fail=False):
        self.key, self.fail, self.calls, self.posts = key, fail, 0, []

    def publish(self, post, video):
        self.calls += 1
        self.posts.append(post)
        if self.fail:
            raise PublishError(f"{self.key} is down")
        return PublishResult(remote_id=f"{self.key}-{post.id}", url=f"https://{self.key}/{post.id}")


class Stubs(dict):
    """Creates one StubPublisher per account/platform on demand."""

    def __call__(self, target, config):
        return self.setdefault(target.key, StubPublisher(target.key))


def make_queue(project, extra=""):
    (project / "videos" / "a.mp4").write_bytes(b"video")
    (project / "videos" / "b.mp4").write_bytes(b"video")
    (project / "queue.yaml").write_text(
        """
- {id: due, video: a.mp4, publish_at: "2026-10-05 17:00", title: Due, caption: Hi}
- {id: later, video: b.mp4, publish_at: "2026-10-09 17:00", title: Later}
""" + extra
    )


def keys(outcomes):
    return sorted(o.target.key for o in outcomes)


def test_publishes_due_posts_to_every_account_and_never_twice(config, project):
    make_queue(project)
    stubs = Stubs()

    outcomes = run_once(config, now=DUE, publisher_factory=stubs)
    # `second` has a 60 minute delay, so only `main` goes out now.
    assert keys(outcomes) == ["main/instagram", "main/tiktok", "main/x", "main/youtube"]
    assert all(o.post_id == "due" and o.ok for o in outcomes)
    assert run_once(config, now=DUE, publisher_factory=stubs) == []

    an_hour_later = run_once(config, now=DUE + timedelta(minutes=60), publisher_factory=stubs)
    assert keys(an_hour_later) == ["second/tiktok", "second/youtube"]

    assert all(s.calls == 1 for s in stubs.values())
    assert State(config.state_file).get("due", "main/youtube")["url"] == "https://main/youtube/due"


def test_post_can_target_one_account_and_some_platforms(config, project):
    make_queue(project, "- {id: only, video: a.mp4, publish_at: '2026-10-05 17:00', title: T, "
                        "accounts: [second], platforms: [youtube, instagram]}\n")
    outcomes = run_once(config, now=DUE + timedelta(hours=2), publisher_factory=Stubs())
    assert [o.target.key for o in outcomes if o.post_id == "only"] == ["second/youtube"]


def test_per_account_text_is_applied(config, project):
    make_queue(project, """- id: custom
  video: a.mp4
  publish_at: "2026-10-05 17:00"
  title: Shared
  caption: Shared caption
  per_account:
    second: {title: Second title, youtube: {description: Second desc}}
""")
    stubs = Stubs()
    run_once(config, now=DUE + timedelta(hours=2), publisher_factory=stubs)
    main_post = [p for p in stubs["main/youtube"].posts if p.id == "custom"][0]
    second_post = [p for p in stubs["second/youtube"].posts if p.id == "custom"][0]
    assert (main_post.youtube_title(), main_post.youtube_description()) == ("Shared", "Shared caption")
    assert (second_post.youtube_title(), second_post.youtube_description()) == ("Second title", "Second desc")


def test_one_target_failing_does_not_block_others_and_stops_after_max_attempts(config, project):
    make_queue(project)
    stubs = Stubs()
    stubs["main/instagram"] = StubPublisher("main/instagram", fail=True)

    first = run_once(config, now=DUE, publisher_factory=stubs)
    assert {o.target.key: o.ok for o in first} == {
        "main/youtube": True, "main/instagram": False, "main/tiktok": True, "main/x": True}

    retry_at = DUE + timedelta(minutes=config.retry_delay_minutes - 1)
    assert [o.target.key for o in run_once(config, now=retry_at, publisher_factory=stubs)] == []

    later = DUE + timedelta(minutes=config.retry_delay_minutes + 1)
    second = run_once(config, now=later, publisher_factory=stubs)
    assert [(o.target.key, o.ok) for o in second] == [("main/instagram", False)]

    much_later = later + timedelta(minutes=config.retry_delay_minutes + 1)
    third = run_once(config, now=much_later, publisher_factory=stubs)
    assert "main/instagram" not in keys(third)  # max_attempts = 2
    assert stubs["main/instagram"].calls == 2

    State(config.state_file).reset("due", "main/instagram")
    stubs["main/instagram"].fail = False
    fourth = run_once(config, now=much_later, publisher_factory=stubs)
    assert [(o.target.key, o.ok) for o in fourth] == [("main/instagram", True)]


def test_dry_run_publishes_nothing(config, project):
    make_queue(project)

    def boom(target, cfg):
        raise AssertionError("dry run must not create publishers")

    outcomes = run_once(config, now=DUE, dry_run=True, publisher_factory=boom)
    assert len(outcomes) == 4
    assert not config.state_file.exists()


def test_missing_video_is_reported_not_crashed(config, project):
    (project / "queue.yaml").write_text("- {id: x, video: nope.mp4, publish_at: '2026-10-05 17:00', title: X}\n")
    outcomes = run_once(config, now=DUE, publisher_factory=Stubs())
    assert outcomes and not any(o.ok for o in outcomes)
    assert "not found" in outcomes[0].detail


def test_x_text_too_long_only_fails_x(config, project):
    long = "word " * 80
    make_queue(project, f"- {{id: long, video: a.mp4, publish_at: '2026-10-05 17:00', title: T, caption: '{long}'}}\n")
    outcomes = [o for o in run_once(config, now=DUE, publisher_factory=Stubs()) if o.post_id == "long"]
    assert {o.target.key: o.ok for o in outcomes} == {
        "main/youtube": True, "main/instagram": True, "main/tiktok": True, "main/x": False}


def test_not_logged_in_waits_without_using_up_attempts(config, project):
    from socialq.platforms import NotLoggedIn

    make_queue(project)
    stubs = Stubs()

    class LoggedOut(StubPublisher):
        logged_in = False

        def publish(self, post, video):
            if not self.logged_in:
                raise NotLoggedIn("x (main): not logged in")
            return super().publish(post, video)

    stubs["main/x"] = LoggedOut("main/x")
    now = DUE
    for _ in range(5):  # well past max_attempts
        run_once(config, now=now, publisher_factory=stubs)
        now += timedelta(minutes=config.retry_delay_minutes + 1)
    entry = State(config.state_file).get("due", "main/x")
    assert entry["status"] == "waiting" and entry.get("attempts", 0) == 0

    stubs["main/x"].logged_in = True
    outcomes = run_once(config, now=now, publisher_factory=stubs)
    assert ("main/x", True) in [(o.target.key, o.ok) for o in outcomes]
