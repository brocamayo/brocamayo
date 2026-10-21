import pytest
import base64
import json

from socialq.platforms import youtube as yt

CHANNELS = {
    "UC_silent": {"id": "UC_silent", "snippet": {"title": "The Silent Catalyst", "customUrl": "@thesilentcatalyst"}},
    "UC_broc": {"id": "UC_broc", "snippet": {"title": "Broc Mayo", "customUrl": "@brocmayo"}},
}


class FakeYouTube:
    def __init__(self, mine_id):
        self.mine_id = mine_id
        self.kwargs = {}

    def channels(self):
        return self

    def list(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        if self.kwargs.get("mine"):
            return {"items": [CHANNELS[self.mine_id]]}
        handle = self.kwargs["forHandle"]
        return {"items": [c for c in CHANNELS.values() if c["snippet"]["customUrl"] == handle]}


class FakeCreds:
    def __init__(self, email):
        body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
        self.id_token = f"h.{body}.s"


def login(monkeypatch, config, account, mine_id, answer="y", want=None, email="me@gmail.com"):
    token = config.path(config.settings(account, "youtube")["token_file"])

    def fake_credentials(cfg, acct, interactive=False, login_hint=None):
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("{}")
        return FakeCreds(email)

    monkeypatch.setattr(yt, "_credentials", fake_credentials)
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: FakeYouTube(mine_id))
    def fake_input(prompt):
        if answer is None:
            raise AssertionError("should not ask when --channel is given")
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return yt.authorize(config, account, want=want), token


def test_login_shows_email_and_channel(monkeypatch, config, capsys):
    ok, _ = login(monkeypatch, config, "main", "UC_broc", email="brocmayo@gmail.com")
    out = capsys.readouterr().out
    assert ok and "Signed in as:  brocmayo@gmail.com" in out and "Broc Mayo (@brocmayo)" in out
    assert yt.saved_channel(config, "main")["id"] == "UC_broc"


def test_wanted_handle_matches_without_asking(monkeypatch, config):
    ok, _ = login(monkeypatch, config, "main", "UC_broc", answer=None, want="brocmayo")
    assert ok and yt.saved_channel(config, "main")["title"] == "Broc Mayo"


def test_wanted_handle_mismatch_explains_which_row_to_pick(monkeypatch, config, capsys):
    ok, token = login(monkeypatch, config, "main", "UC_silent", answer=None, want="@brocmayo")
    out = capsys.readouterr().out
    assert not ok and not token.exists()
    assert "@brocmayo is the channel named \"Broc Mayo\"" in out
    assert "--google-id" in out


def test_same_channel_warning_and_saying_no_logs_out(monkeypatch, config, capsys):
    login(monkeypatch, config, "main", "UC_broc")
    capsys.readouterr()
    login(monkeypatch, config, "second", "UC_broc", answer="y")
    assert "account 'main' is logged in to this same channel" in capsys.readouterr().out
    ok, token = login(monkeypatch, config, "second", "UC_silent", answer="n")
    assert not ok and not token.exists()


def test_accounts_command_shows_channel(monkeypatch, config, project, capsys):
    from socialq.cli import main

    login(monkeypatch, config, "main", "UC_broc")
    capsys.readouterr()
    main(["-c", str(project / "config.yaml"), "accounts"])
    out = capsys.readouterr().out
    assert "youtube    logged in to channel: Broc Mayo" in out


def test_google_id_is_passed_as_login_hint(monkeypatch, config, project):
    import google_auth_oauthlib.flow as gflow

    (project / "credentials" / "youtube_client_secret.json").write_text("{}")
    seen = {}

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            return cls()

        def run_local_server(self, **kwargs):
            seen.update(kwargs)
            raise KeyboardInterrupt  # stop before any real network

    monkeypatch.setattr(gflow, "InstalledAppFlow", FakeFlow)
    try:
        yt._credentials(config, "main", interactive=True, login_hint="110447739688482854681")
    except KeyboardInterrupt:
        pass
    assert seen["login_hint"] == "110447739688482854681" and seen["prompt"] == "consent"


def test_upload_resumes_after_dropped_connection(monkeypatch, config, project):
    import googleapiclient.discovery
    import googleapiclient.http
    from socialq.platforms.base import PublishError
    from socialq.queue import load_queue

    (project / "videos" / "v.mp4").write_bytes(b"x" * 10)
    (project / "queue.yaml").write_text("- {id: v, video: v.mp4, publish_at: '2026-10-01 09:00', title: T}\n")
    [post] = load_queue(config)
    monkeypatch.setattr(yt, "_credentials", lambda cfg, acct, **k: object())
    monkeypatch.setattr(yt, "_upload_http", lambda creds: None)
    monkeypatch.setattr(googleapiclient.http, "MediaFileUpload", lambda *a, **k: object())
    monkeypatch.setattr(yt.time, "sleep", lambda s: None)

    class Request:
        def __init__(self, outcomes):
            self.outcomes = outcomes

        def next_chunk(self, num_retries):
            item = self.outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    class Service:
        def __init__(self, outcomes):
            self.req = Request(outcomes)

        def videos(self):
            return self

        def insert(self, **kwargs):
            return self.req

    # Two drops, then success: the upload is resumed, not failed, and each resume first asks
    # YouTube how much it received (googleapiclient's error-state status query).
    outcomes = [BrokenPipeError(32, "Broken pipe"), TimeoutError("timed out"), (None, {"id": "vid1"})]
    service = Service(outcomes)
    error_state_on_call = []
    original = Request.next_chunk

    def tracking_next_chunk(self, num_retries):
        error_state_on_call.append(getattr(self, "_in_error_state", False))
        return original(self, num_retries)

    monkeypatch.setattr(Request, "next_chunk", tracking_next_chunk)
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **k: service)
    result = yt.YouTubePublisher(config, "main").publish(post, post.video_path(config))
    assert result.remote_id == "vid1"
    assert error_state_on_call == [False, True, True]

    # A connection that never recovers becomes a clean PublishError (retried on a later run).
    outcomes = [BrokenPipeError(32, "Broken pipe")] * (yt.NETWORK_RETRIES + 1)
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **k: Service(list(outcomes)))
    with pytest.raises(PublishError, match="kept losing its connection"):
        yt.YouTubePublisher(config, "main").publish(post, post.video_path(config))


def test_upload_http_keeps_308_as_resume_and_has_a_timeout():
    """YouTube answers each uploaded chunk with 308; it must not be followed as a redirect."""
    from google.oauth2.credentials import Credentials

    http = yt._upload_http(Credentials(token="t"))
    assert 308 not in http.http.redirect_codes
    assert http.http.timeout == yt.UPLOAD_TIMEOUT_S


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class PlaylistYouTube:
    def __init__(self, playlists):
        self.playlists_data, self.inserted = playlists, []

    def playlists(self):
        return self

    def list(self, **kwargs):
        return _Exec({"items": self.playlists_data})

    def list_next(self, request, resp):
        return None

    def playlistItems(self):
        return self

    def insert(self, part, body):
        self.inserted.append(body["snippet"])
        return _Exec({})


def make_publisher(config, scopes):
    pub = yt.YouTubePublisher(config, "main")
    pub._creds = type("C", (), {"scopes": scopes})()
    return pub


def test_playlist_added_by_name(config):
    service = PlaylistYouTube([{"id": "PL1", "snippet": {"title": "Shorts"}},
                               {"id": "PL2", "snippet": {"title": "AI + Stocks"}}])
    make_publisher(config, yt.SCOPES)._add_to_playlist(service, "vid1", "ai + stocks")
    assert service.inserted == [{"playlistId": "PL2", "resourceId": {"kind": "youtube#video", "videoId": "vid1"}}]


def test_playlist_skipped_with_hint_when_login_lacks_scope(config, caplog):
    service = PlaylistYouTube([{"id": "PL2", "snippet": {"title": "AI + Stocks"}}])
    make_publisher(config, ["https://www.googleapis.com/auth/youtube.upload"])._add_to_playlist(
        service, "vid1", "AI + Stocks")
    assert service.inserted == [] and "predates playlist support" in caplog.text


def test_missing_playlist_is_a_warning_not_a_failure(config, caplog):
    make_publisher(config, yt.SCOPES)._add_to_playlist(PlaylistYouTube([]), "vid1", "Nope")
    assert "no playlist named 'Nope'" in caplog.text


def test_add_command_stores_playlist(project):
    from socialq.cli import main
    from socialq.config import load_config
    from socialq.queue import load_queue

    (project / "videos" / "v.mp4").write_bytes(b"x")
    main(["-c", str(project / "config.yaml"), "add", str(project / "videos" / "v.mp4"),
          "--title", "T", "--playlist", "AI + Stocks", "--at", "2026-10-01 09:00", "--accounts", "main"])
    [post] = load_queue(load_config(project / "config.yaml"))
    assert post.option("youtube", "playlist") == "AI + Stocks"


def test_add_command_accepts_comma_separated_accounts(project):
    from socialq.cli import main
    from socialq.config import load_config
    from socialq.queue import load_queue

    (project / "videos" / "v.mp4").write_bytes(b"x")
    main(["-c", str(project / "config.yaml"), "add", str(project / "videos" / "v.mp4"),
          "--title", "T", "--at", "2026-10-01 09:00", "--accounts", "main,second", "--platforms", "youtube"])
    config = load_config(project / "config.yaml")
    [post] = load_queue(config)
    assert sorted(t.account for t in post.targets(config)) == ["main", "second"]


class CommentYouTube:
    """Fake client for videos().list and commentThreads().insert."""

    def __init__(self, privacy):
        self.privacy, self.comments = privacy, []

    def videos(self):
        return self

    def commentThreads(self):
        return self

    def list(self, part, id):
        return _Exec({"items": [{"id": id, "status": {"privacyStatus": self.privacy}}]})

    def insert(self, part, body):
        self.comments.append(body["snippet"])
        return _Exec({"id": f"c{len(self.comments)}"})


def published_post_with_comment(project, config):
    from datetime import datetime, timezone
    from socialq.queue import load_queue
    from socialq.state import State

    (project / "queue.yaml").write_text(
        "- id: clip\n  video: clip.mp4\n  publish_at: '2026-10-05 17:00'\n  accounts: [main]\n"
        "  platforms: [youtube]\n  title: T\n  youtube: {comment: 'Find your date: example.com'}\n")
    state = State(config.state_file)
    state.record_success("clip", "main/youtube", "vid9", "https://youtu.be/vid9",
                         at=datetime(2026, 10, 5, tzinfo=timezone.utc))
    return load_queue(config), state


def test_comment_waits_for_public_then_posts_once(project, config):
    from datetime import datetime, timezone

    posts, state = published_post_with_comment(project, config)
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    service = CommentYouTube("private")
    yt.post_pending_comments(config, state, posts, now, client_for=lambda c, a: service)
    assert service.comments == [] and state.get("clip", "main/youtube").get("comment_status") is None

    service.privacy = "public"
    yt.post_pending_comments(config, state, posts, now, client_for=lambda c, a: service)
    yt.post_pending_comments(config, state, posts, now, client_for=lambda c, a: service)
    assert service.comments == [{"videoId": "vid9",
                                 "topLevelComment": {"snippet": {"textOriginal": "Find your date: example.com"}}}]
    assert state.get("clip", "main/youtube")["comment_status"] == "posted"


def test_comment_waits_for_a_login_with_comment_permission(project, config, caplog):
    from datetime import datetime, timezone

    posts, state = published_post_with_comment(project, config)

    def old_login(cfg, account):
        raise yt.NoCommentScope("youtube (main): this login predates comment support. Run `x` once")

    yt.post_pending_comments(config, state, posts, datetime(2026, 10, 6, tzinfo=timezone.utc), client_for=old_login)
    assert "predates comment support" in caplog.text
    assert yt.pending_comments(config, state, posts)


def test_comment_gives_up_on_a_video_that_stays_private(project, config):
    from datetime import datetime, timezone

    posts, state = published_post_with_comment(project, config)
    service = CommentYouTube("private")
    yt.post_pending_comments(config, state, posts, datetime(2026, 12, 1, tzinfo=timezone.utc),
                             client_for=lambda c, a: service)
    assert state.get("clip", "main/youtube")["comment_status"] == "gave_up"
    assert not yt.pending_comments(config, state, posts)


def test_add_command_stores_comment_and_playlist(project):
    from socialq.cli import main
    from socialq.config import load_config
    from socialq.queue import load_queue

    (project / "videos" / "v.mp4").write_bytes(b"x")
    main(["-c", str(project / "config.yaml"), "add", str(project / "videos" / "v.mp4"), "--title", "T",
          "--playlist", "Shorts", "--comment", "Pin me", "--at", "2026-10-01 09:00", "--accounts", "main"])
    [post] = load_queue(load_config(project / "config.yaml"))
    assert post.option("youtube", "playlist") == "Shorts" and post.option("youtube", "comment") == "Pin me"
