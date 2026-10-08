import json
import time

import pytest

from conftest import FakeResponse, FakeSession
from socialq.platforms.base import PublishError
from socialq.platforms.instagram import InstagramPublisher
from socialq.platforms.tiktok import MB, TikTokPublisher, chunk_ranges, plan_chunks
from socialq.platforms.x import XPublisher
from socialq.queue import load_queue


def one_post(project, config, platforms, extra=""):
    (project / "videos" / "clip.mp4").write_bytes(b"0123456789")
    (project / "queue.yaml").write_text(
        f"- id: clip\n  video: clip.mp4\n  publish_at: '2026-10-05 17:00'\n"
        f"  platforms: {platforms}\n  title: Clip\n  caption: Hello\n  tags: [money]\n{extra}"
    )
    [post] = load_queue(config)
    return post, post.video_path(config)


# --- TikTok chunking -----------------------------------------------------------

@pytest.mark.parametrize("size", [1, 5 * MB - 1, 64 * MB])
def test_small_files_upload_in_one_chunk(size):
    assert plan_chunks(size) == (size, 1)


def test_large_file_leftover_goes_in_last_chunk():
    size = 200 * MB + 123
    chunk, count = plan_chunks(size, 10 * MB)
    assert (chunk, count) == (10 * MB, 20)
    ranges = chunk_ranges(size, chunk, count)
    assert ranges[0] == (0, 10 * MB - 1)
    assert ranges[-1] == (190 * MB, size - 1)
    assert sum(e - s + 1 for s, e in ranges) == size
    assert all(e - s + 1 <= 128 * MB for s, e in ranges)


def test_chunk_size_is_clamped_to_tiktok_limits():
    assert plan_chunks(300 * MB, 1 * MB)[0] == 5 * MB
    assert plan_chunks(300 * MB, 100 * MB)[0] == 64 * MB


# --- TikTok publishing ---------------------------------------------------------

def save_tiktok_token(project):
    (project / "credentials" / "main").mkdir(exist_ok=True)
    (project / "credentials" / "main" / "tiktok_token.json").write_text(json.dumps({
        "access_token": "tt-token", "refresh_token": "r", "open_id": "o",
        "expires_at": time.time() + 3600, "refresh_expires_at": time.time() + 86400,
    }))


def test_tiktok_direct_post_flow(project, config):
    save_tiktok_token(project)
    post, video = one_post(project, config, "[tiktok]")
    ok = {"error": {"code": "ok"}}
    session = FakeSession([
        ("POST", "creator_info", FakeResponse(200, {**ok, "data": {
            "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "duet_disabled": True}})),
        ("POST", "/video/init/", FakeResponse(200, {**ok, "data": {
            "publish_id": "p1", "upload_url": "https://upload.tiktok/abc"}})),
        ("PUT", "upload.tiktok", FakeResponse(201)),
        ("POST", "status/fetch", [
            FakeResponse(200, {**ok, "data": {"status": "PROCESSING_UPLOAD"}}),
            FakeResponse(200, {**ok, "data": {"status": "PUBLISH_COMPLETE",
                                              "publicaly_available_post_id": [7123]}}),
        ]),
    ])
    result = TikTokPublisher(config, "main", session=session).publish(post, video)
    assert result.remote_id == "7123"

    init = next(c for c in session.calls if "/video/init/" in c[1])[2]["json"]
    assert init["post_info"]["title"] == "Hello\n\n#money"
    assert init["post_info"]["disable_duet"] is True  # creator setting wins
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 10,
                                   "chunk_size": 10, "total_chunk_count": 1}
    put = next(c for c in session.calls if c[0] == "PUT")[2]
    assert put["headers"]["Content-Range"] == "bytes 0-9/10"
    assert put["data"] == b"0123456789"
    assert all(c[2]["headers"]["Authorization"] == "Bearer tt-token"
               for c in session.calls if c[0] == "POST")


def test_tiktok_rejects_privacy_the_app_is_not_allowed(project, config):
    save_tiktok_token(project)
    post, video = one_post(project, config, "[tiktok]")
    session = FakeSession([("POST", "creator_info", FakeResponse(200, {
        "error": {"code": "ok"}, "data": {"privacy_level_options": ["SELF_ONLY"]}}))])
    with pytest.raises(PublishError, match="mode: inbox"):
        TikTokPublisher(config, "main", session=session).publish(post, video)


def test_tiktok_inbox_mode(project, config):
    save_tiktok_token(project)
    post, video = one_post(project, config, "[tiktok]", "  tiktok: {mode: inbox}\n")
    ok = {"error": {"code": "ok"}}
    session = FakeSession([
        ("POST", "/inbox/video/init/", FakeResponse(200, {**ok, "data": {
            "publish_id": "p2", "upload_url": "https://upload.tiktok/x"}})),
        ("PUT", "upload.tiktok", FakeResponse(201)),
        ("POST", "status/fetch", FakeResponse(200, {**ok, "data": {"status": "SEND_TO_USER_INBOX"}})),
    ])
    assert TikTokPublisher(config, "main", session=session).publish(post, video).remote_id == "p2"
    assert not any("creator_info" in c[1] for c in session.calls)


def test_tiktok_api_error_is_surfaced(project, config):
    save_tiktok_token(project)
    post, video = one_post(project, config, "[tiktok]", "  tiktok: {mode: inbox}\n")
    session = FakeSession([("POST", "/inbox/video/init/", FakeResponse(
        403, {"error": {"code": "scope_not_authorized", "message": "nope"}}))])
    with pytest.raises(PublishError, match="scope_not_authorized"):
        TikTokPublisher(config, "main", session=session).publish(post, video)


def test_tiktok_requires_login(project, config):
    post, video = one_post(project, config, "[tiktok]")
    with pytest.raises(PublishError, match="auth tiktok"):
        TikTokPublisher(config, "main", session=FakeSession([])).publish(post, video)


# --- Instagram -----------------------------------------------------------------

def save_instagram_token(project):
    (project / "credentials" / "main").mkdir(exist_ok=True)
    (project / "credentials" / "main" / "instagram_token.json").write_text(json.dumps({
        "ig_user_id": "1789", "access_token": "ig-token", "expires_at": time.time() + 30 * 86400}))

def test_instagram_reel_flow(project, config):
    save_instagram_token(project)
    post, video = one_post(project, config, "[instagram]", "  instagram: {thumb_offset_ms: 1500}\n")
    session = FakeSession([
        ("POST", "/1789/media_publish", FakeResponse(200, {"id": "media-1"})),
        ("POST", "/1789/media", FakeResponse(200, {"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"})),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "/c1", [FakeResponse(200, {"status_code": "IN_PROGRESS"}),
                        FakeResponse(200, {"status_code": "FINISHED"})]),
        ("GET", "/media-1", FakeResponse(200, {"permalink": "https://instagram.com/reel/abc"})),
    ])
    result = InstagramPublisher(config, "main", session=session).publish(post, video)
    assert (result.remote_id, result.url) == ("media-1", "https://instagram.com/reel/abc")

    create = session.calls[0][2]["data"]
    assert create["media_type"] == "REELS" and create["upload_type"] == "resumable"
    assert create["caption"] == "Hello\n\n#money" and create["thumb_offset"] == "1500"
    upload = session.calls[1][2]
    assert upload["headers"] == {"Authorization": "OAuth ig-token", "offset": "0", "file_size": "10"}
    assert upload["data"] == b"0123456789"


def test_instagram_processing_error(project, config):
    save_instagram_token(project)
    post, video = one_post(project, config, "[instagram]")
    session = FakeSession([
        ("POST", "/1789/media", FakeResponse(200, {"id": "c1"})),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "/c1", FakeResponse(200, {"status_code": "ERROR", "status": "Error: bad codec"})),
    ])
    with pytest.raises(PublishError, match="bad codec"):
        InstagramPublisher(config, "main", session=session).publish(post, video)


def test_retries_rate_limits(project, config):
    save_instagram_token(project)
    post, video = one_post(project, config, "[instagram]")
    session = FakeSession([
        ("POST", "/1789/media", [FakeResponse(429, {"error": "slow down"}), FakeResponse(400, {"error": "bad"})]),
    ])
    with pytest.raises(PublishError, match="bad"):
        InstagramPublisher(config, "main", session=session).publish(post, video)
    assert len(session.calls) == 2


def test_instagram_publish_is_not_retried_on_server_error(project, config):
    save_instagram_token(project)
    post, video = one_post(project, config, "[instagram]")
    session = FakeSession([
        ("POST", "/1789/media_publish", FakeResponse(500, {"error": "maybe posted"})),
        ("POST", "/1789/media", FakeResponse(200, {"id": "c1"})),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "/c1", FakeResponse(200, {"status_code": "FINISHED"})),
    ])
    with pytest.raises(PublishError, match="publish failed"):
        InstagramPublisher(config, "main", session=session).publish(post, video)
    assert sum("media_publish" in c[1] for c in session.calls) == 1


def test_instagram_requires_login_per_account(project, config):
    post, video = one_post(project, config, "[instagram]")
    with pytest.raises(PublishError, match="auth instagram --account second"):
        InstagramPublisher(config, "second", session=FakeSession([])).publish(post, video)


# --- X ---------------------------------------------------------------------------

def save_x_token(project, account="main", expires_in=3600):
    (project / "credentials" / account).mkdir(exist_ok=True)
    (project / "credentials" / account / "x_token.json").write_text(json.dumps({
        "access_token": f"x-{account}", "refresh_token": "r1", "expires_at": time.time() + expires_in}))


def test_x_video_post_flow(project, config):
    save_x_token(project)
    post, video = one_post(project, config, "[x]", "  x: {caption: 'Short and sweet https://example.com/a-very-long-link'}\n")
    session = FakeSession([
        ("POST", "/media/upload/initialize", FakeResponse(200, {"data": {"id": "m1"}})),
        ("POST", "/media/upload/m1/append", FakeResponse(204)),
        ("POST", "/media/upload/m1/finalize", FakeResponse(200, {"data": {
            "id": "m1", "processing_info": {"state": "pending", "check_after_secs": 1}}})),
        ("GET", "/media/upload?", [
            FakeResponse(200, {"data": {"processing_info": {"state": "in_progress", "check_after_secs": 1}}}),
            FakeResponse(200, {"data": {"processing_info": {"state": "succeeded"}}}),
        ]),
        ("POST", "/2/tweets", FakeResponse(201, {"data": {"id": "999", "text": "..."}})),
    ])
    result = XPublisher(config, "main", session=session).publish(post, video)
    assert (result.remote_id, result.url) == ("999", "https://x.com/i/status/999")

    init = session.calls[0][2]["json"]
    assert init == {"media_type": "video/mp4", "total_bytes": 10, "media_category": "tweet_video"}
    append = session.calls[1][2]
    assert append["data"] == {"segment_index": "0"} and append["files"]["media"][1] == b"0123456789"
    tweet = session.calls[-1][2]["json"]
    assert tweet == {"text": "Short and sweet https://example.com/a-very-long-link", "media": {"media_ids": ["m1"]}}
    assert all(c[2]["headers"]["Authorization"] == "Bearer x-main" for c in session.calls)


def test_x_processing_failure(project, config):
    save_x_token(project)
    post, video = one_post(project, config, "[x]")
    session = FakeSession([
        ("POST", "/media/upload/initialize", FakeResponse(200, {"data": {"id": "m1"}})),
        ("POST", "/media/upload/m1/append", FakeResponse(204)),
        ("POST", "/media/upload/m1/finalize", FakeResponse(200, {"data": {
            "processing_info": {"state": "failed", "error": {"message": "InvalidMedia"}}}})),
    ])
    with pytest.raises(PublishError, match="InvalidMedia"):
        XPublisher(config, "main", session=session).publish(post, video)


def test_x_refreshes_expired_token_and_keeps_new_refresh_token(project, config, monkeypatch):
    from socialq.platforms import x as xmod

    save_x_token(project, expires_in=-10)
    sent = {}

    def fake_post(url, data, headers, timeout):
        sent.update(data)
        return FakeResponse(200, {"access_token": "fresh", "refresh_token": "r2", "expires_in": 7200})

    monkeypatch.setattr(xmod.requests, "post", fake_post)
    assert xmod.access_token(config, "main") == "fresh"
    assert sent == {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": "x-client"}
    saved = json.loads((project / "credentials" / "main" / "x_token.json").read_text())
    assert saved["refresh_token"] == "r2"


# --- Instagram API with Instagram Login (no Facebook Page) -------------------------

def test_instagram_login_token_is_saved_with_username_and_host(project, config, monkeypatch):
    from socialq.platforms import instagram as ig

    calls = []

    def fake_get(url, params, timeout):
        calls.append(url)
        if url.endswith("/me"):
            return FakeResponse(200, {"user_id": "17841", "username": "brocmayo"})
        if url.endswith("/refresh_access_token"):
            return FakeResponse(200, {"access_token": "IGlong", "expires_in": 5184000})
        raise AssertionError(url)

    monkeypatch.delenv("INSTAGRAM_APP_SECRET", raising=False)
    monkeypatch.setattr(ig.requests, "get", fake_get)
    ig.authorize(config, "main", "IGshort", ig_username="@brocmayo")
    saved = json.loads((project / "credentials" / "main" / "instagram_token.json").read_text())
    assert saved["host"] == "graph.instagram.com" and saved["username"] == "brocmayo"
    assert saved["ig_user_id"] == "17841" and saved["access_token"] == "IGlong"
    assert calls[0] == "https://graph.instagram.com/v25.0/me"


def test_instagram_login_token_for_wrong_account_is_rejected(config, monkeypatch):
    from socialq.platforms import instagram as ig

    monkeypatch.setattr(ig.requests, "get", lambda url, params, timeout: FakeResponse(
        200, {"user_id": "1", "username": "snowballerapp"}))
    with pytest.raises(PublishError, match="for @snowballerapp, not @brocmayo"):
        ig.authorize(config, "main", "IGshort", ig_username="brocmayo")


def test_instagram_login_posts_via_instagram_host_and_renews_before_expiry(project, config, monkeypatch):
    from socialq.platforms import instagram as ig

    (project / "credentials" / "main").mkdir(exist_ok=True)
    token_file = project / "credentials" / "main" / "instagram_token.json"
    token_file.write_text(json.dumps({"ig_user_id": "17841", "access_token": "IGold", "host": "graph.instagram.com",
                                      "expires_at": time.time() + 3 * 86400}))
    monkeypatch.setattr(ig.requests, "get", lambda url, params, timeout: FakeResponse(
        200, {"access_token": "IGnew", "expires_in": 5184000}))
    post, video = one_post(project, config, "[instagram]")
    session = FakeSession([
        ("POST", "graph.instagram.com/v25.0/17841/media_publish", FakeResponse(200, {"id": "m9"})),
        ("POST", "graph.instagram.com/v25.0/17841/media", FakeResponse(200, {"id": "c1", "uri": "https://rupload.facebook.com/x/c1"})),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "graph.instagram.com/v25.0/c1", FakeResponse(200, {"status_code": "FINISHED"})),
        ("GET", "graph.instagram.com/v25.0/m9", FakeResponse(200, {"permalink": "https://instagram.com/reel/z"})),
    ])
    result = InstagramPublisher(config, "main", session=session).publish(post, video)
    assert result.url == "https://instagram.com/reel/z"
    assert session.calls[0][2]["data"]["access_token"] == "IGnew"
    saved = json.loads(token_file.read_text())
    assert saved["access_token"] == "IGnew" and saved["expires_at"] > time.time() + 50 * 86400


def test_tiktok_login_asks_only_for_scopes_the_mode_needs(project, config, monkeypatch):
    from socialq.platforms import tiktok as tt

    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    (project / "config.yaml").write_text((project / "config.yaml").read_text().replace(
        "tiktok: {mode: direct, chunk_size_mb: 5}", "tiktok: {mode: direct, chunk_size_mb: 5, redirect_uri: 'https://example.com/'}"))
    from socialq.config import load_config
    cfg = load_config(project / "config.yaml")
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt))
    for account, expected in (("second", "user.info.basic%2Cvideo.upload&"), ("main", "video.publish")):
        printed.clear()
        with pytest.raises(KeyboardInterrupt):
            tt.authorize(cfg, account)
        url = next(line for line in printed if "tiktok.com/v2/auth/authorize" in line)
        assert expected in url
