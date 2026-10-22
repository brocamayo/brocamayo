import json
import struct

import pytest
import requests

from conftest import FakeResponse, FakeSession
from socialq.media import probe
from socialq.platforms.base import NotLoggedIn, PublishError
from socialq.platforms.facebook import FacebookPublisher
from socialq.queue import load_queue


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mvhd(seconds: float, timescale: int = 1000) -> bytes:
    return box(b"mvhd", bytes(4) + struct.pack(">IIII", 0, 0, timescale, int(seconds * timescale)) + bytes(80))


def tkhd(width: int, height: int, rotate: bool = False) -> bytes:
    a, b, c, d = (0, 0x10000, -0x10000, 0) if rotate else (0x10000, 0, 0, 0x10000)
    matrix = struct.pack(">9i", a, b, 0, c, d, 0, 0, 0, 0x40000000)
    body = bytes(4) + bytes(20) + bytes(8) + bytes(8) + matrix + struct.pack(">II", width << 16, height << 16)
    return box(b"tkhd", body)


def mp4(seconds: float, width: int, height: int, rotate: bool = False, moov_last: bool = True) -> bytes:
    moov = box(b"moov", mvhd(seconds) + box(b"trak", tkhd(0, 0)) + box(b"trak", tkhd(width, height, rotate)))
    parts = [box(b"ftyp", b"isom" + bytes(4)), box(b"mdat", b"\x00" * 64)]
    return b"".join(parts + [moov] if moov_last else [parts[0], moov, parts[1]])


# --- reading the video header -------------------------------------------------------

@pytest.mark.parametrize("moov_last", [True, False])
def test_probe_reads_duration_and_size(tmp_path, moov_last):
    f = tmp_path / "v.mp4"
    f.write_bytes(mp4(26.7, 1080, 1920, moov_last=moov_last))
    info = probe(f)
    assert (round(info.duration_s, 1), info.width, info.height, info.vertical) == (26.7, 1080, 1920, True)


def test_probe_handles_phone_rotation(tmp_path):
    f = tmp_path / "v.mov"
    f.write_bytes(mp4(10, 1920, 1080, rotate=True))
    assert probe(f).vertical


def test_probe_gives_up_on_non_mp4(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"not a video at all")
    assert probe(f) is None


# --- publishing -------------------------------------------------------------------------

def fb_post(project, config, video_bytes, extra=""):
    (project / "videos" / "clip.mp4").write_bytes(video_bytes)
    (project / "queue.yaml").write_text(
        "- id: clip\n  video: clip.mp4\n  publish_at: '2026-10-05 17:00'\n"
        "  platforms: [facebook]\n  title: Clip title\n  caption: \"Hello #Shorts\"\n  tags: [money]\n" + extra)
    (project / "credentials" / "main").mkdir(parents=True, exist_ok=True)
    (project / "credentials" / "main" / "facebook_token.json").write_text(json.dumps(
        {"page_id": "555", "page_name": "Snowballer", "access_token": "page-token"}))
    [post] = load_queue(config)
    return post, post.video_path(config)


def test_short_vertical_video_posts_as_reel(project, config):
    post, video = fb_post(project, config, mp4(27, 1080, 1920))
    session = FakeSession([
        ("POST", "/555/video_reels", [FakeResponse(200, {"video_id": "v1", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/v1"}),
                                      FakeResponse(200, {"success": True})]),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "/v1", [FakeResponse(200, {"status": {"video_status": "processing"}}),
                        FakeResponse(200, {"status": {"publishing_phase": {"status": "complete"}}}),
                        FakeResponse(200, {"permalink_url": "/reel/v1/"})]),
    ])
    result = FacebookPublisher(config, "main", session=session).publish(post, video)
    assert (result.remote_id, result.url) == ("v1", "https://www.facebook.com/reel/v1/")

    upload = session.calls[1][2]
    assert upload["headers"] == {"Authorization": "OAuth page-token", "offset": "0", "file_size": str(video.stat().st_size)}
    finish = session.calls[2][2]["data"]
    assert finish["upload_phase"] == "finish" and finish["video_state"] == "PUBLISHED"
    assert finish["description"] == "Hello\n\n#money"   # #Shorts dropped, tags added


def test_reel_upload_resumes_after_a_dropped_connection(project, config):
    post, video = fb_post(project, config, mp4(27, 1080, 1920))
    size = video.stat().st_size
    session = FakeSession([
        ("POST", "/555/video_reels", [FakeResponse(200, {"video_id": "v1"}), FakeResponse(200, {"success": True})]),
        ("POST", "rupload.facebook.com", [requests.ConnectionError("reset"), FakeResponse(200, {"success": True})]),
        ("GET", "/v1", [FakeResponse(200, {"status": {"uploading_phase": {"bytes_transferred": 40}}}),
                        FakeResponse(200, {"status": {"video_status": "ready"}}),
                        FakeResponse(200, {})]),
    ])
    real_request = session.request

    def request(method, url, **kw):
        resp = real_request(method, url, **kw)
        if isinstance(resp, Exception):
            raise resp
        return resp

    session.request = request
    result = FacebookPublisher(config, "main", session=session).publish(post, video)
    assert result.url == "https://www.facebook.com/reel/v1"
    uploads = [c for c in session.calls if "rupload" in c[1]]
    assert uploads[1][2]["headers"]["offset"] == "40"
    assert uploads[1][2]["data"] == video.read_bytes()[40:]
    assert size > 40


def test_reel_processing_error_is_reported(project, config):
    post, video = fb_post(project, config, mp4(27, 1080, 1920))
    session = FakeSession([
        ("POST", "/555/video_reels", [FakeResponse(200, {"video_id": "v1"}), FakeResponse(200, {"success": True})]),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "/v1", FakeResponse(200, {"status": {"processing_phase": {"status": "error", "errors": [{"message": "bad audio"}]}}})),
    ])
    with pytest.raises(PublishError, match="bad audio"):
        FacebookPublisher(config, "main", session=session).publish(post, video)


def test_long_horizontal_video_posts_as_regular_video_in_chunks(project, config):
    post, video = fb_post(project, config, mp4(430, 1920, 1080))
    size = video.stat().st_size
    session = FakeSession([
        ("POST", "graph-video.facebook.com/v25.0/555/videos", [
            FakeResponse(200, {"upload_session_id": "s1", "video_id": "v2", "start_offset": "0", "end_offset": "100"}),
            FakeResponse(200, {"start_offset": "100", "end_offset": str(size)}),
            FakeResponse(200, {"start_offset": str(size), "end_offset": str(size)}),
            FakeResponse(200, {"success": True}),
        ]),
        ("GET", "/v2", FakeResponse(200, {"permalink_url": "https://www.facebook.com/555/videos/v2/"})),
    ])
    result = FacebookPublisher(config, "main", session=session).publish(post, video)
    assert (result.remote_id, result.url) == ("v2", "https://www.facebook.com/555/videos/v2/")
    phases = [c[2]["data"]["upload_phase"] for c in session.calls if c[0] == "POST"]
    assert phases == ["start", "transfer", "transfer", "finish"]
    chunks = [c[2]["files"]["video_file_chunk"][1] for c in session.calls if "files" in c[2]]
    assert b"".join(chunks) == video.read_bytes()
    finish = session.calls[3][2]["data"]
    assert finish["title"] == "Clip title" and finish["description"] == "Hello\n\n#money"


def test_finish_is_not_retried_on_server_error(project, config):
    post, video = fb_post(project, config, mp4(27, 1080, 1920))
    session = FakeSession([
        ("POST", "/555/video_reels", [FakeResponse(200, {"video_id": "v1"}), FakeResponse(500, {"error": "maybe"})]),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
    ])
    with pytest.raises(PublishError, match="publishing the Reel failed"):
        FacebookPublisher(config, "main", session=session).publish(post, video)
    assert sum(c[2]["data"].get("upload_phase") == "finish" for c in session.calls if c[0] == "POST" and "data" in c[2]
               and isinstance(c[2]["data"], dict)) == 1


def test_as_option_forces_the_kind(project, config):
    post, video = fb_post(project, config, mp4(27, 1080, 1920), "  facebook: {as: video}\n")
    from socialq.platforms.facebook import as_reel
    assert not as_reel(post, video, {})
    post, video = fb_post(project, config, b"unreadable", "  facebook: {as: reel}\n")
    assert as_reel(post, video, {})


def test_needs_login(project, config):
    post, video = fb_post(project, config, mp4(27, 1080, 1920))
    (project / "credentials" / "second").mkdir(exist_ok=True)
    with pytest.raises(NotLoggedIn, match="auth facebook --account second"):
        FacebookPublisher(config, "second", session=FakeSession([])).publish(post, video)


# --- login ------------------------------------------------------------------------------

def test_auth_saves_the_chosen_page_token(project, config, monkeypatch, capsys):
    from socialq.platforms import facebook as fb

    def fake_get(url, params, timeout):
        if url.endswith("/oauth/access_token"):
            assert params["fb_exchange_token"] == "short"
            return FakeResponse(200, {"access_token": "long"})
        if url.endswith("/me/accounts"):
            assert params["access_token"] == "long"
            return FakeResponse(200, {"data": [
                {"id": "1", "name": "Broc", "access_token": "pt-1", "tasks": ["CREATE_CONTENT"]},
                {"id": "2", "name": "Snowballer", "access_token": "pt-2", "tasks": ["CREATE_CONTENT"]}]})
        raise AssertionError(url)

    monkeypatch.setenv("META_APP_ID", "app")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setattr(fb.requests, "get", fake_get)
    fb.authorize(config, "main", "short", page="snowballer")
    saved = json.loads((project / "credentials" / "main" / "facebook_token.json").read_text())
    assert saved == {"page_id": "2", "page_name": "Snowballer", "access_token": "pt-2"}

    fb.authorize(config, "second", "short", page="Snowballer")
    assert "same Page" in capsys.readouterr().out

    with pytest.raises(PublishError, match="found: Broc, Snowballer"):
        fb.authorize(config, "main", "short", page="Nope")


def test_auth_needs_app_keys(config, monkeypatch):
    from socialq.platforms import facebook as fb
    monkeypatch.delenv("META_APP_ID", raising=False)
    with pytest.raises(PublishError, match="META_APP_ID"):
        fb.authorize(config, "main", "short")
