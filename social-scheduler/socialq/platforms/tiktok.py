"""TikTok via the Content Posting API (chunked FILE_UPLOAD).

Two modes:
  direct - posts straight to the profile (needs the `video.publish` scope; until
           TikTok audits your app, only SELF_ONLY/private posts are allowed)
  inbox  - sends the video to your TikTok inbox as a draft; you tap Post in the
           app (needs `video.upload`; works without an audit)
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from ..queue import Post
from .base import NotLoggedIn, PublishError, PublishResult, Publisher, auth_hint, error_text, log, poll

API = "https://open.tiktokapis.com/v2"
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
# inbox mode only needs video.upload; direct mode also needs video.publish (Direct Post product).
SCOPES = {"inbox": "user.info.basic,video.upload", "direct": "user.info.basic,video.upload,video.publish"}


def scopes_for(mode: str) -> str:
    return SCOPES.get(mode, SCOPES["inbox"])

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
DEFAULT_CHUNK = 10 * MB


def plan_chunks(video_size: int, preferred: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) following TikTok's rules.

    Files up to 64 MB go in one chunk. Larger files use `preferred`-sized chunks;
    the remainder is folded into the last chunk (allowed up to 128 MB).
    """
    if video_size <= 0:
        raise PublishError("tiktok: video file is empty")
    if video_size <= MAX_CHUNK:
        return video_size, 1
    chunk = min(max(preferred, MIN_CHUNK), MAX_CHUNK)
    return chunk, video_size // chunk


def chunk_ranges(video_size: int, chunk_size: int, count: int) -> list[tuple[int, int]]:
    """Inclusive (start, end) byte ranges; the last chunk takes any leftover bytes."""
    ranges = []
    for i in range(count):
        start = i * chunk_size
        end = video_size - 1 if i == count - 1 else start + chunk_size - 1
        ranges.append((start, end))
    return ranges


# --- OAuth ------------------------------------------------------------------

def _client() -> tuple[str, str]:
    key, secret = os.environ.get("TIKTOK_CLIENT_KEY"), os.environ.get("TIKTOK_CLIENT_SECRET")
    if not key or not secret:
        raise PublishError("tiktok: set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env")
    return key, secret


def _token_file(config, account: str) -> Path:
    return config.path(config.settings(account, "tiktok")["token_file"])


def _save_token(config, account: str, data: dict) -> dict:
    now = int(time.time())
    token = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "open_id": data.get("open_id"),
        "scope": data.get("scope"),
        "expires_at": now + int(data.get("expires_in", 86400)),
        "refresh_expires_at": now + int(data.get("refresh_expires_in", 365 * 86400)),
    }
    path = _token_file(config, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2))
    return token


def _token_request(payload: dict) -> dict:
    resp = requests.post(f"{API}/oauth/token/", data=payload, timeout=60,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    data = resp.json() if resp.content else {}
    if not resp.ok or "access_token" not in data:
        raise PublishError(f"tiktok: token request failed: {error_text(resp)}")
    return data


def access_token(config, account: str) -> str:
    path = _token_file(config, account)
    if not path.exists():
        raise NotLoggedIn(f"tiktok ({account}): not logged in. Run `{auth_hint('tiktok', account, config)}`.")
    token = json.loads(path.read_text())
    if token["expires_at"] - time.time() > 300:
        return token["access_token"]
    if token["refresh_expires_at"] < time.time():
        raise NotLoggedIn(f"tiktok ({account}): login expired. Run `{auth_hint('tiktok', account, config)}`.")
    key, secret = _client()
    log.info("tiktok (%s): refreshing access token", account)
    data = _token_request({"client_key": key, "client_secret": secret,
                           "grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
    return _save_token(config, account, data)["access_token"]


def authorize(config, account: str) -> None:
    key, secret = _client()
    redirect_uri = config.settings(account, "tiktok").get("redirect_uri")
    if not redirect_uri:
        raise PublishError("tiktok: set tiktok.redirect_uri in config.yaml (must match your TikTok app)")
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE_URL + "?" + urlencode({
        "client_key": key, "scope": scopes_for(config.settings(account, "tiktok").get("mode", "inbox")),
        "response_type": "code",
        "redirect_uri": redirect_uri, "state": state,
    })
    print(f"Logging in TikTok for account '{account}'.")
    print("Tip: if you have two TikTok accounts, open the link in a private/incognito window")
    print("     so you log in to the right one.\n")
    print("1. Open this link and approve access:\n\n   " + url + "\n")
    print("2. You'll land on your redirect page. Copy the FULL address from the browser bar.")
    pasted = input("3. Paste it here: ").strip()
    query = parse_qs(urlparse(pasted).query)
    if query.get("state", [None])[0] != state:
        raise PublishError("tiktok: state mismatch - paste the URL from this login attempt")
    if "code" not in query:
        raise PublishError(f"tiktok: no code in that URL ({query.get('error_description', query)})")
    data = _token_request({"client_key": key, "client_secret": secret, "code": query["code"][0],
                           "grant_type": "authorization_code", "redirect_uri": redirect_uri})
    _save_token(config, account, data)
    print(f"TikTok logged in for '{account}'.")


# --- publishing ---------------------------------------------------------------

class TikTokPublisher(Publisher):
    name = "tiktok"

    def _api(self, path: str, token: str, body: dict) -> dict:
        resp = self.request("POST", f"{API}{path}", json=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        })
        payload = resp.json() if resp.content else {}
        err = payload.get("error", {})
        if not resp.ok or err.get("code", "ok") != "ok":
            raise PublishError(f"tiktok: {path} failed: {err.get('code')} {err.get('message') or error_text(resp)}")
        return payload.get("data", {})

    def publish(self, post: Post, video: Path) -> PublishResult:
        s = self.settings
        mode = post.option("tiktok", "mode", s.get("mode", "inbox"))
        token = access_token(self.config, self.account)
        size = video.stat().st_size
        chunk_size, count = plan_chunks(size, int(s.get("chunk_size_mb", 10)) * MB)
        source_info = {"source": "FILE_UPLOAD", "video_size": size,
                       "chunk_size": chunk_size, "total_chunk_count": count}

        if mode == "direct":
            creator = self._api("/post/publish/creator_info/query/", token, {})
            privacy = post.option("tiktok", "privacy_level", s.get("privacy_level", "PUBLIC_TO_EVERYONE"))
            allowed = creator.get("privacy_level_options") or []
            if allowed and privacy not in allowed:
                raise PublishError(
                    f"tiktok: privacy_level {privacy} isn't allowed for this account/app "
                    f"(allowed: {allowed}). Unaudited apps can only post SELF_ONLY; "
                    "use mode: inbox until TikTok approves your app."
                )
            post_info = {
                "title": post.social_caption("tiktok"),
                "privacy_level": privacy,
                "disable_comment": bool(creator.get("comment_disabled") or s.get("disable_comment", False)),
                "disable_duet": bool(creator.get("duet_disabled") or s.get("disable_duet", False)),
                "disable_stitch": bool(creator.get("stitch_disabled") or s.get("disable_stitch", False)),
                "video_cover_timestamp_ms": int(post.option("tiktok", "cover_timestamp_ms", 1000)),
            }
            if s.get("is_aigc"):
                post_info["is_aigc"] = True
            init = self._api("/post/publish/video/init/", token,
                             {"post_info": post_info, "source_info": source_info})
            done_statuses = {"PUBLISH_COMPLETE"}
        elif mode == "inbox":
            init = self._api("/post/publish/inbox/video/init/", token, {"source_info": source_info})
            done_statuses = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}
        else:
            raise PublishError(f"tiktok: unknown mode {mode!r} (use 'direct' or 'inbox')")

        publish_id, upload_url = init["publish_id"], init["upload_url"]
        with video.open("rb") as fh:
            for i, (start, end) in enumerate(chunk_ranges(size, chunk_size, count), 1):
                fh.seek(start)
                data = fh.read(end - start + 1)
                log.info("tiktok: uploading chunk %d/%d", i, count)
                resp = self.request("PUT", upload_url, data=data, timeout=600, headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                })
                if resp.status_code not in (200, 201, 206):
                    raise PublishError(f"tiktok: chunk {i} upload failed: HTTP {resp.status_code} {error_text(resp)}")

        result: dict = {}

        def finished() -> bool:
            result.update(self._api("/post/publish/status/fetch/", token, {"publish_id": publish_id}))
            status = result.get("status")
            if status == "FAILED":
                raise PublishError(f"tiktok: publish failed: {result.get('fail_reason')}")
            return status in done_statuses

        poll(finished, timeout_s=float(s.get("processing_timeout_s", 900)), interval_s=10,
             what="TikTok to process the video")

        post_ids = result.get("publicaly_available_post_id") or []  # (sic) TikTok's field name
        if post_ids:
            return PublishResult(remote_id=str(post_ids[0]))
        if mode == "inbox":
            log.info("tiktok: sent to your TikTok inbox - open the app to finish posting")
        return PublishResult(remote_id=publish_id)
