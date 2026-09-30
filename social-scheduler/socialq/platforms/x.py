"""X (Twitter) via the X API v2: chunked media upload, then a post with the video attached.

X API access is pay-per-use: you buy credits in the X developer console and
each post costs a small fee.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from ..queue import Post
from .base import NotLoggedIn, PublishError, PublishResult, Publisher, auth_hint, error_text, log, poll

API = "https://api.x.com/2"
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = f"{API}/oauth2/token"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"
CHUNK_SIZE = 4 * 1024 * 1024


# --- OAuth 2.0 (PKCE) -----------------------------------------------------------

def _client() -> tuple[str, str | None]:
    client_id = os.environ.get("X_CLIENT_ID")
    if not client_id:
        raise PublishError("x: set X_CLIENT_ID (and X_CLIENT_SECRET) in .env")
    return client_id, os.environ.get("X_CLIENT_SECRET") or None


def _token_file(config, account: str) -> Path:
    return config.path(config.settings(account, "x")["token_file"])


def _token_request(payload: dict) -> dict:
    client_id, client_secret = _client()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:  # confidential client ("Web App"): HTTP Basic auth
        raw = f"{client_id}:{client_secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    resp = requests.post(TOKEN_URL, data={**payload, "client_id": client_id}, headers=headers, timeout=60)
    data = resp.json() if resp.content else {}
    if not resp.ok or "access_token" not in data:
        raise PublishError(f"x: token request failed: {error_text(resp)}")
    return data


def _save_token(config, account: str, data: dict, username: str | None = None) -> dict:
    path = _token_file(config, account)
    old = json.loads(path.read_text()) if path.exists() else {}
    token = {
        "access_token": data["access_token"],
        # X rotates refresh tokens: every refresh returns a new one.
        "refresh_token": data.get("refresh_token", old.get("refresh_token")),
        "expires_at": int(time.time()) + int(data.get("expires_in", 7200)),
        "username": username or old.get("username"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2))
    return token


def access_token(config, account: str) -> str:
    path = _token_file(config, account)
    if not path.exists():
        raise NotLoggedIn(f"x ({account}): not logged in. Run `{auth_hint('x', account, config)}`.")
    token = json.loads(path.read_text())
    if token["expires_at"] - time.time() > 300:
        return token["access_token"]
    if not token.get("refresh_token"):
        raise NotLoggedIn(f"x ({account}): login expired. Run `{auth_hint('x', account, config)}`.")
    log.info("x (%s): refreshing access token", account)
    data = _token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
    return _save_token(config, account, data)["access_token"]


def authorize(config, account: str) -> None:
    redirect_uri = config.settings(account, "x").get("redirect_uri")
    if not redirect_uri:
        raise PublishError("x: set x.redirect_uri in config.yaml (must match your X app's callback URL)")
    client_id, _ = _client()
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print(f"Logging in X for account '{account}'.")
    print("Tip: if you have two X accounts, open the link in a private/incognito window")
    print("     so you log in to the right one.\n")
    print("1. Open this link and approve access:\n\n   " + url + "\n")
    print("2. You'll land on your redirect page. Copy the FULL address from the browser bar.")
    query = parse_qs(urlparse(input("3. Paste it here: ").strip()).query)
    if query.get("state", [None])[0] != state:
        raise PublishError("x: state mismatch - paste the URL from this login attempt")
    if "code" not in query:
        raise PublishError(f"x: no code in that URL ({query.get('error', query)})")
    data = _token_request({"grant_type": "authorization_code", "code": query["code"][0],
                           "redirect_uri": redirect_uri, "code_verifier": verifier})
    me = requests.get(f"{API}/users/me", headers={"Authorization": f"Bearer {data['access_token']}"}, timeout=30)
    username = me.json().get("data", {}).get("username") if me.ok else None
    _save_token(config, account, data, username)
    print(f"X logged in for '{account}'" + (f" as @{username}." if username else "."))


# --- publishing ---------------------------------------------------------------

class XPublisher(Publisher):
    name = "x"

    def _call(self, method: str, path: str, token: str, **kwargs) -> dict:
        resp = self.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
        if not resp.ok:
            raise PublishError(f"x: {path.split('?')[0]} failed: HTTP {resp.status_code} {error_text(resp)}")
        return (resp.json() if resp.content else {}).get("data", {})

    def publish(self, post: Post, video: Path) -> PublishResult:
        token = access_token(self.config, self.account)
        size = video.stat().st_size

        media = self._call("POST", "/media/upload/initialize", token, json={
            "media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video",
        })
        media_id = str(media["id"])

        with video.open("rb") as fh:
            index = 0
            while chunk := fh.read(CHUNK_SIZE):
                log.info("x: uploading chunk %d/%d", index + 1, -(-size // CHUNK_SIZE))
                self._call("POST", f"/media/upload/{media_id}/append", token,
                           files={"media": ("chunk", chunk, "application/octet-stream")},
                           data={"segment_index": str(index)}, timeout=600)
                index += 1

        info = self._call("POST", f"/media/upload/{media_id}/finalize", token).get("processing_info")

        def processed() -> bool:
            nonlocal info
            if not info:
                return True
            state = info.get("state")
            if state == "failed":
                raise PublishError(f"x: video processing failed: {(info.get('error') or {}).get('message', info)}")
            if state == "succeeded":
                return True
            time.sleep(min(int(info.get("check_after_secs", 5)), 30))
            info = self._call("GET", f"/media/upload?{urlencode({'command': 'STATUS', 'media_id': media_id})}",
                              token).get("processing_info")
            return False

        poll(processed, timeout_s=float(self.settings.get("processing_timeout_s", 900)), interval_s=0,
             what="X to process the video")

        tweet = self._call("POST", "/tweets", token, creates=True, json={
            "text": post.social_caption("x"), "media": {"media_ids": [media_id]},
        })
        return PublishResult(remote_id=str(tweet["id"]), url=f"https://x.com/i/status/{tweet['id']}")
