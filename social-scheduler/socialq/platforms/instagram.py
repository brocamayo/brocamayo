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


def load_credentials(config, account: str) -> tuple[str, str]:
    """Return (ig_user_id, access_token) saved by `socialq auth instagram`."""
    token_file = config.path(config.settings(account, "instagram")["token_file"])
    if not token_file.exists():
        raise NotLoggedIn(f"instagram ({account}): not logged in. "
                           f"Run `{auth_hint('instagram', account, config)} --token <token>` (see README)")
    saved = json.loads(token_file.read_text())
    expires_at = saved.get("expires_at")
    if expires_at and expires_at - time.time() < 7 * 86400:
        log.warning("instagram (%s): login expires in under 7 days; run `%s --refresh`",
                    account, auth_hint("instagram", account, config))
    if not saved.get("ig_user_id") or not saved.get("access_token"):
        raise PublishError(f"instagram ({account}): {token_file} is missing ig_user_id or access_token")
    return saved["ig_user_id"], saved["access_token"]


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
    base = _base_url(config.settings(account, "instagram"))
    app_id, app_secret = os.environ.get("META_APP_ID"), os.environ.get("META_APP_SECRET")
    if not app_id or not app_secret:
        raise PublishError("instagram: set META_APP_ID and META_APP_SECRET in .env first")
    token_file = config.path(config.settings(account, "instagram")["token_file"])
    saved = json.loads(token_file.read_text()) if token_file.exists() else {}
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
        base = _base_url(s)
        user_id, token = load_credentials(self.config, self.account)

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
