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
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from ..queue import Post
from .base import NotLoggedIn, PublishError, PublishResult, Publisher, TryLater, auth_hint, error_text, log, poll

API = "https://open.tiktokapis.com/v2"
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
# inbox mode only needs video.upload; direct mode also needs video.publish (Direct Post product).
SCOPES = {"inbox": "user.info.basic,video.upload", "direct": "user.info.basic,video.upload,video.publish"}


def scopes_for(mode: str) -> str:
    return SCOPES.get(mode, SCOPES["inbox"])

MB = 1024 * 1024
SLOW_CHUNK_S = 45          # TikTok drops a chunk request that runs past about a minute
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
DEFAULT_CHUNK = 5 * MB
CHUNK_ATTEMPTS = 3


def plan_chunks(video_size: int, preferred: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) following TikTok's rules.

    Files under 5 MB (or no bigger than one chunk) go up whole. Otherwise they're
    split into `preferred`-sized chunks, kept small so each request finishes quickly
    on a slow connection; the remainder is folded into the last chunk.
    """
    if video_size <= 0:
        raise PublishError("tiktok: video file is empty")
    chunk = min(max(preferred, MIN_CHUNK), MAX_CHUNK)
    if video_size <= chunk:
        return video_size, 1
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


def _save_token(config, account: str, data: dict, previous: dict | None = None) -> dict:
    now = int(time.time())
    previous = previous or {}
    token = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        # A refresh reply may leave these out; keep what the login saved.
        "open_id": data.get("open_id") or previous.get("open_id"),
        "scope": data.get("scope") or previous.get("scope"),
        "expires_at": now + int(data.get("expires_in", 86400)),
        "refresh_expires_at": now + int(data.get("refresh_expires_in", 365 * 86400)),
    }
    if previous.get("display_name"):
        token["display_name"] = previous["display_name"]
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
    fresh = _save_token(config, account, data, previous=token)
    if not fresh.get("open_id") or not fresh.get("display_name"):
        # Logins refreshed by older versions lost these; look them up again.
        user = _user_info(fresh["access_token"])
        if user.get("open_id") or user.get("display_name"):
            fresh["open_id"] = fresh.get("open_id") or user.get("open_id")
            if user.get("display_name"):
                fresh["display_name"] = user["display_name"]
            path.write_text(json.dumps(fresh, indent=2))
    return fresh["access_token"]


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
    token = _save_token(config, account, data)
    name = _display_name(token["access_token"])
    if name:
        token["display_name"] = name
        _token_file(config, account).write_text(json.dumps(token, indent=2))
    print(f"TikTok logged in for '{account}'" + (f" as {name}." if name else "."))
    for other in config.accounts:
        if other == account or not _token_file(config, other).exists():
            continue
        if token.get("open_id") and json.loads(_token_file(config, other).read_text()).get("open_id") == token["open_id"]:
            print(f"\n⚠️  That's the same TikTok account '{other}' is logged in to. Run this again in a")
            print("   private window and log in to the other TikTok account.")


def _user_info(access_token: str) -> dict:
    """{open_id, display_name} for a login (best effort; only shown to the user)."""
    try:
        resp = requests.get(f"{API}/user/info/", params={"fields": "open_id,display_name"}, timeout=30,
                            headers={"Authorization": f"Bearer {access_token}"})
        return resp.json().get("data", {}).get("user", {}) or {}
    except (requests.RequestException, ValueError, AttributeError):
        return {}


def _display_name(access_token: str) -> str | None:
    return _user_info(access_token).get("display_name") or None


# --- publishing ---------------------------------------------------------------

def _copy_to_clipboard(text: str) -> bool:
    """Best effort, macOS only. With Universal Clipboard it can be pasted straight into TikTok on an iPhone."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _push_caption(account: str, post_id: str, caption: str) -> bool:
    """Send the caption to your phone through ntfy (NTFY_TOPIC in .env), so you can copy it
    from the notification when you post the draft. Best effort."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        resp = requests.post(f"{server}/{topic}", data=caption.encode(), timeout=15, headers={
            "Title": f"TikTok draft ready ({account}): {post_id}".encode(),
            "Tags": "memo",
        })
        return resp.ok
    except requests.RequestException as exc:
        log.warning("tiktok: couldn't send the caption to your phone: %s", exc)
        return False


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
            problem = f"tiktok: {path} failed: {err.get('code')} {err.get('message') or error_text(resp)}"
            if err.get("code") in ("spam_risk_too_many_pending_share", "rate_limit_exceeded"):
                # Too many drafts waiting in the inbox, or too many calls: clears up on its own.
                raise TryLater(problem + " (post or delete the drafts waiting in your TikTok inbox)")
            raise PublishError(problem)
        return payload.get("data", {})

    def _put_chunk(self, url: str, data: bytes, start: int, end: int, size: int, i: int, count: int) -> None:
        """Upload one chunk, retrying it a couple of times (TikTok accepts a re-sent chunk)."""
        for attempt in range(1, CHUNK_ATTEMPTS + 1):
            log.info("tiktok: uploading chunk %d/%d (%.1f MB)", i, count, len(data) / MB)
            began = time.monotonic()
            try:
                resp = self.request("PUT", url, data=data, timeout=300, headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                })
            except PublishError as exc:  # network error after request()'s own retries
                problem = str(exc)
            else:
                if resp.status_code in (200, 201, 206):
                    log.info("tiktok: chunk %d/%d done in %ds", i, count, time.monotonic() - began)
                    return
                took = time.monotonic() - began
                logid = resp.headers.get("x-tt-logid")
                problem = (f"HTTP {resp.status_code} {error_text(resp)} after {int(took)}s"
                           + (f" (logid {logid})" if logid else ""))
                if resp.status_code == 403 and took >= SLOW_CHUNK_S:
                    # TikTok timed the chunk out: the connection is too slow right now. Re-sending it
                    # straight away would only time out again, so leave it for a later run.
                    mbps = len(data) * 8 / took / 1e6
                    raise TryLater(f"tiktok: upload too slow right now ({len(data) / MB:.0f} MB took "
                                   f"{int(took)}s, about {mbps:.1f} Mbps); trying again later")
            if attempt == CHUNK_ATTEMPTS:
                raise PublishError(f"tiktok: chunk {i}/{count} upload failed: {problem}")
            log.warning("tiktok: chunk %d/%d failed (%s), retrying", i, count, problem)
            time.sleep(5 * attempt)

    def publish(self, post: Post, video: Path) -> PublishResult:
        s = self.settings
        mode = post.option("tiktok", "mode", s.get("mode", "inbox"))
        token = access_token(self.config, self.account)
        size = video.stat().st_size
        chunk_size, count = plan_chunks(size, int(s.get("chunk_size_mb", DEFAULT_CHUNK // MB)) * MB)
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
                self._put_chunk(upload_url, fh.read(end - start + 1), start, end, size, i, count)

        result: dict = {}

        def finished() -> bool:
            result.update(self._api("/post/publish/status/fetch/", token, {"publish_id": publish_id}))
            status = result.get("status")
            if status == "FAILED":
                raise PublishError(f"tiktok: publish failed: {result.get('fail_reason')}")
            return status in done_statuses

        log.info("tiktok: upload done, waiting for TikTok to process it")
        poll(finished, timeout_s=float(s.get("processing_timeout_s", 900)), interval_s=10,
             what="TikTok to process the video")

        post_ids = result.get("publicaly_available_post_id") or []  # (sic) TikTok's field name
        if post_ids:
            return PublishResult(remote_id=str(post_ids[0]))
        if mode == "inbox":
            # Inbox uploads can't carry a caption; TikTok has you type it when you tap Post.
            caption = post.social_caption("tiktok")
            log.info("tiktok: sent to your TikTok inbox - open the app to finish posting")
            if caption:
                copied = _copy_to_clipboard(caption)
                pushed = _push_caption(self.account, post.id, caption)
                where = [w for w, ok in (("copied to your clipboard", copied), ("sent to your phone", pushed)) if ok]
                log.info("tiktok: caption to paste%s:\n%s", f" ({', '.join(where)})" if where else "", caption)
        return PublishResult(remote_id=publish_id)
