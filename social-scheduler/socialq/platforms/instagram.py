"""Instagram Reels via the Instagram Graph API (resumable upload, no public URL needed)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from ..queue import Post
from .base import NotLoggedIn, PublishError, PublishResult, Publisher, auth_hint, error_text, log, poll

DEFAULT_API_VERSION = "v25.0"
UPLOAD_HOST = "https://rupload.facebook.com/ig-api-upload"


def _base_url(settings: dict) -> str:
    return f"https://{settings.get('graph_host', 'graph.facebook.com')}/{settings.get('api_version', DEFAULT_API_VERSION)}"


IG_LOGIN_HOST = "graph.instagram.com"
IG_TOKEN_LIFETIME = 60 * 86400


def _token_path(config, account: str) -> Path:
    return config.path(config.settings(account, "instagram")["token_file"])


def _refresh_ig_login_token(token: str) -> dict | None:
    """Extend an Instagram-Login token by another 60 days (only works once it's >24h old)."""
    resp = requests.get(f"https://{IG_LOGIN_HOST}/refresh_access_token",
                        params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=60)
    return resp.json() if resp.ok and "access_token" in resp.json() else None


def load_credentials(config, account: str) -> tuple[str, str, str]:
    """Return (ig_user_id, access_token, api_host) saved by `socialq auth instagram`."""
    token_file = _token_path(config, account)
    if not token_file.exists():
        raise NotLoggedIn(f"instagram ({account}): not logged in. "
                          f"Run `{auth_hint('instagram', account, config)} --token <token>` (see README)")
    saved = json.loads(token_file.read_text())
    if not saved.get("ig_user_id") or not saved.get("access_token"):
        raise PublishError(f"instagram ({account}): {token_file} is missing ig_user_id or access_token")
    host = saved.get("host") or config.settings(account, "instagram").get("graph_host", "graph.facebook.com")
    expires_at = saved.get("expires_at")
    if expires_at and expires_at < time.time():
        raise NotLoggedIn(f"instagram ({account}): login expired. "
                          f"Run `{auth_hint('instagram', account, config)} --token <new token>`")
    if expires_at and expires_at - time.time() < 10 * 86400:
        if host == IG_LOGIN_HOST and (fresh := _refresh_ig_login_token(saved["access_token"])):
            saved.update(access_token=fresh["access_token"],
                         expires_at=int(time.time()) + int(fresh.get("expires_in", IG_TOKEN_LIFETIME)))
            token_file.write_text(json.dumps(saved, indent=2))
            log.info("instagram (%s): login extended for another 60 days", account)
        else:
            log.warning("instagram (%s): login expires soon; run `%s --refresh`",
                        account, auth_hint("instagram", account, config))
    return saved["ig_user_id"], saved["access_token"], host


def authorize_instagram_login(config, account: str, token: str, ig_username: str | None = None) -> None:
    """Save a token from Meta's "API setup with Instagram login" (no Facebook Page needed)."""
    version = config.settings(account, "instagram").get("api_version", DEFAULT_API_VERSION)
    me = requests.get(f"https://{IG_LOGIN_HOST}/{version}/me",
                      params={"fields": "user_id,username", "access_token": token}, timeout=60)
    if not me.ok:
        raise PublishError(f"instagram: that token didn't work: {error_text(me)}")
    info = me.json()
    username = info.get("username")
    if ig_username and username and ig_username.lstrip("@").lower() != username.lower():
        raise PublishError(f"instagram: that token is for @{username}, not @{ig_username.lstrip('@')}")

    expires_in = None
    secret = os.environ.get("INSTAGRAM_APP_SECRET")
    if secret:  # swap a short-lived (1 hour) token for a 60-day one
        resp = requests.get(f"https://{IG_LOGIN_HOST}/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": secret, "access_token": token}, timeout=60)
        if resp.ok and "access_token" in resp.json():
            token, expires_in = resp.json()["access_token"], int(resp.json().get("expires_in", IG_TOKEN_LIFETIME))
    if expires_in is None and (fresh := _refresh_ig_login_token(token)):  # already long-lived
        token, expires_in = fresh["access_token"], int(fresh.get("expires_in", IG_TOKEN_LIFETIME))
    if expires_in is None:
        expires_in = IG_TOKEN_LIFETIME
        if not secret:
            print("note: if this token came from the dashboard's Generate token button and stops working")
            print("      within an hour, add INSTAGRAM_APP_SECRET to .env and run this again.")

    token_file = _token_path(config, account)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps({
        "ig_user_id": str(info.get("user_id") or info["id"]), "username": username,
        "access_token": token, "expires_at": int(time.time()) + expires_in, "host": IG_LOGIN_HOST,
    }, indent=2))
    print(f"Instagram logged in for '{account}' as @{username}. It renews itself automatically.")


def _choose_ig_account(base: str, token: str, wanted: str | None) -> str:
    pages = requests.get(f"{base}/me/accounts", params={
        "fields": "name,instagram_business_account{id,username}", "access_token": token, "limit": 100,
    }, timeout=60).json().get("data", [])
    linked = [p["instagram_business_account"] | {"page": p["name"]}
              for p in pages if p.get("instagram_business_account")]
    if not linked:
        raise PublishError("instagram: no Instagram professional account is linked to your Facebook Pages")
    if wanted:
        wanted = wanted.lstrip("@").lower()
        for ig in linked:
            if wanted in (ig["id"], str(ig.get("username", "")).lower()):
                return ig["id"]
        raise PublishError(f"instagram: @{wanted} isn't linked to any of your Facebook Pages "
                           f"(found: {', '.join('@' + str(i.get('username')) for i in linked)})")
    if len(linked) == 1:
        return linked[0]["id"]
    print("Which Instagram account is this?")
    for i, ig in enumerate(linked, 1):
        print(f"  {i}. @{ig.get('username')}  (Page: {ig['page']})")
    choice = input("Number: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(linked):
        raise PublishError("instagram: no account picked")
    return linked[int(choice) - 1]["id"]


def authorize(config, account: str, short_lived_token: str | None, refresh: bool = False,
              ig_username: str | None = None) -> None:
    """Swap a short-lived Meta user token for a ~60-day one and pick the IG account."""
    if short_lived_token and short_lived_token.startswith("IG"):
        return authorize_instagram_login(config, account, short_lived_token, ig_username)
    token_file = _token_path(config, account)
    saved = json.loads(token_file.read_text()) if token_file.exists() else {}
    if refresh and saved.get("host") == IG_LOGIN_HOST:
        fresh = _refresh_ig_login_token(saved["access_token"])
        if not fresh:
            raise PublishError("instagram: couldn't extend the login; generate a new token and run auth again")
        saved.update(access_token=fresh["access_token"],
                     expires_at=int(time.time()) + int(fresh.get("expires_in", IG_TOKEN_LIFETIME)))
        token_file.write_text(json.dumps(saved, indent=2))
        print(f"Instagram login for '{account}' extended.")
        return
    base = _base_url(config.settings(account, "instagram"))
    app_id, app_secret = os.environ.get("META_APP_ID"), os.environ.get("META_APP_SECRET")
    if not app_id or not app_secret:
        raise PublishError("instagram: set META_APP_ID and META_APP_SECRET in .env first "
                           "(or use a token from 'API setup with Instagram login', which starts with IG)")
    source = short_lived_token or (saved.get("access_token") if refresh else None)
    if not source:
        raise PublishError("instagram: pass --token <short-lived token> (or --refresh)")

    resp = requests.get(f"{base}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": source,
    }, timeout=60)
    if not resp.ok:
        raise PublishError(f"instagram: token exchange failed: {error_text(resp)}")
    data = resp.json()
    token = data["access_token"]
    expires_at = int(time.time()) + int(data.get("expires_in", 60 * 86400))

    ig_user_id = saved.get("ig_user_id") if refresh and not ig_username else None
    ig_user_id = ig_user_id or _choose_ig_account(base, token, ig_username)

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(
        {"ig_user_id": ig_user_id, "access_token": token, "expires_at": expires_at}, indent=2))
    print(f"Instagram logged in for '{account}' (IG id {ig_user_id}); "
          f"expires {time.strftime('%Y-%m-%d', time.localtime(expires_at))}.")


class InstagramPublisher(Publisher):
    name = "instagram"

    def publish(self, post: Post, video: Path) -> PublishResult:
        s = self.settings
        version = s.get("api_version", DEFAULT_API_VERSION)
        user_id, token, host = load_credentials(self.config, self.account)
        base = f"https://{host}/{version}"

        # 1. Create a Reels container that expects a resumable upload.
        params = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": post.social_caption("instagram"),
            "share_to_feed": str(post.option("instagram", "share_to_feed", s.get("share_to_feed", True))).lower(),
            "access_token": token,
        }
        thumb_offset = post.option("instagram", "thumb_offset_ms")
        if thumb_offset is not None:
            params["thumb_offset"] = str(thumb_offset)
        resp = self.request("POST", f"{base}/{user_id}/media", data=params)
        if not resp.ok:
            raise PublishError(f"instagram: creating container failed: {error_text(resp)}")
        container_id = resp.json()["id"]
        upload_uri = resp.json().get("uri") or f"{UPLOAD_HOST}/{version}/{container_id}"

        # 2. Upload the video bytes.
        size = video.stat().st_size
        log.info("instagram: uploading %.1f MB", size / 1e6)
        with video.open("rb") as fh:
            resp = self.request("POST", upload_uri, retries=0, timeout=900, data=fh, headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
            })
        if not resp.ok:
            raise PublishError(f"instagram: upload failed: {error_text(resp)}")

        # 3. Wait for Instagram to finish processing.
        def ready() -> bool:
            r = self.request("GET", f"{base}/{container_id}",
                             params={"fields": "status_code,status", "access_token": token})
            if not r.ok:
                raise PublishError(f"instagram: status check failed: {error_text(r)}")
            body = r.json()
            code = body.get("status_code")
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"instagram: processing failed ({code}): {body.get('status')}")
            return code in ("FINISHED", "PUBLISHED")

        poll(ready, timeout_s=float(s.get("processing_timeout_s", 900)), interval_s=10,
             what="Instagram to process the video")

        # 4. Publish it.
        resp = self.request("POST", f"{base}/{user_id}/media_publish", creates=True,
                            data={"creation_id": container_id, "access_token": token})
        if not resp.ok:
            raise PublishError(f"instagram: publish failed: {error_text(resp)}")
        media_id = resp.json()["id"]

        url = None
        r = self.request("GET", f"{base}/{media_id}", params={"fields": "permalink", "access_token": token})
        if r.ok:
            url = r.json().get("permalink")
        return PublishResult(remote_id=media_id, url=url)
