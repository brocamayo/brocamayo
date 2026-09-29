import json
import time

import pytest

from conftest import FakeResponse, FakeSession
from socialq.platforms.base import PublishError
from socialq.platforms.instagram import InstagramPublisher
from socialq.platforms.tiktok import MB, TikTokPublisher, chunk_ranges, plan_chunks
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
    (project / "credentials" / "tiktok_token.json").write_text(json.dumps({
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
    result = TikTokPublisher(config, session=session).publish(post, video)
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
        TikTokPublisher(config, session=session).publish(post, video)


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
    assert TikTokPublisher(config, session=session).publish(post, video).remote_id == "p2"
    assert not any("creator_info" in c[1] for c in session.calls)


def test_tiktok_api_error_is_surfaced(project, config):
    save_tiktok_token(project)
    post, video = one_post(project, config, "[tiktok]", "  tiktok: {mode: inbox}\n")
    session = FakeSession([("POST", "/inbox/video/init/", FakeResponse(
        403, {"error": {"code": "scope_not_authorized", "message": "nope"}}))])
    with pytest.raises(PublishError, match="scope_not_authorized"):
        TikTokPublisher(config, session=session).publish(post, video)


def test_tiktok_requires_login(project, config):
    post, video = one_post(project, config, "[tiktok]")
    with pytest.raises(PublishError, match="auth tiktok"):
        TikTokPublisher(config, session=FakeSession([])).publish(post, video)


# --- Instagram -----------------------------------------------------------------

def test_instagram_reel_flow(project, config, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1789")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    post, video = one_post(project, config, "[instagram]", "  instagram: {thumb_offset_ms: 1500}\n")
    session = FakeSession([
        ("POST", "/1789/media_publish", FakeResponse(200, {"id": "media-1"})),
        ("POST", "/1789/media", FakeResponse(200, {"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"})),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "/c1", [FakeResponse(200, {"status_code": "IN_PROGRESS"}),
                        FakeResponse(200, {"status_code": "FINISHED"})]),
        ("GET", "/media-1", FakeResponse(200, {"permalink": "https://instagram.com/reel/abc"})),
    ])
    result = InstagramPublisher(config, session=session).publish(post, video)
    assert (result.remote_id, result.url) == ("media-1", "https://instagram.com/reel/abc")

    create = session.calls[0][2]["data"]
    assert create["media_type"] == "REELS" and create["upload_type"] == "resumable"
    assert create["caption"] == "Hello\n\n#money" and create["thumb_offset"] == "1500"
    upload = session.calls[1][2]
    assert upload["headers"] == {"Authorization": "OAuth ig-token", "offset": "0", "file_size": "10"}
    assert upload["data"] == b"0123456789"


def test_instagram_processing_error(project, config, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1789")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    post, video = one_post(project, config, "[instagram]")
    session = FakeSession([
        ("POST", "/1789/media", FakeResponse(200, {"id": "c1"})),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "/c1", FakeResponse(200, {"status_code": "ERROR", "status": "Error: bad codec"})),
    ])
    with pytest.raises(PublishError, match="bad codec"):
        InstagramPublisher(config, session=session).publish(post, video)


def test_retries_rate_limits(project, config, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_USER_ID", "1789")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    post, video = one_post(project, config, "[instagram]")
    session = FakeSession([
        ("POST", "/1789/media", [FakeResponse(429, {"error": "slow down"}), FakeResponse(400, {"error": "bad"})]),
    ])
    with pytest.raises(PublishError, match="bad"):
        InstagramPublisher(config, session=session).publish(post, video)
    assert len(session.calls) == 2
