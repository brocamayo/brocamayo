"""Instagram Reels via the Instagram Graph API (resumable upload, no public URL needed)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from ..queue import Post
from .base import PublishError, PublishResult, Publisher, error_text, log, poll

DEFAULT_API_VERSION = "v25.0"
UPLOAD_HOST = "https://rupload.facebook.com/ig-api-upload"


def _token_file(config) -> Path:
    return config.path(config.instagram.get("token_file", "credentials/instagram_token.json"))


def load_credentials(config) -> tuple[str, str]:
    """Return (ig_user_id, access_token) from .env, falling back to the token file."""
    user_id = os.environ.get("INSTAGRAM_USER_ID", "")
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    token_file = _token_file(config)
    if token_file.exists():
        saved = json.loads(token_file.read_text())
        user_id = user_id or saved.get("ig_user_id", "")
        token = token or saved.get("access_token", "")
        expires_at = saved.get("expires_at")
        if expires_at and expires_at - time.time() < 7 * 86400:
            log.warning("instagram: access token expires in under 7 days; "
                        "run `python -m socialq auth instagram --refresh`")
    if not user_id or not token:
        raise PublishError(
            "instagram: set INSTAGRAM_USER_ID and INSTAGRAM_ACCESS_TOKEN in .env "
            "or run `python -m socialq auth instagram` (see README)"
        )
    return user_id, token


def authorize(config, short_lived_token: str | None, refresh: bool = False) -> None:
    """Swap a short-lived Meta user token for a ~60-day one and look up the IG account id."""
    s = config.instagram
    base = f"https://{s.get('graph_host', 'graph.facebook.com')}/{s.get('api_version', DEFAULT_API_VERSION)}"
    app_id, app_secret = os.environ.get("META_APP_ID"), os.environ.get("META_APP_SECRET")
    if not app_id or not app_secret:
        raise PublishError("instagram: set META_APP_ID and META_APP_SECRET in .env first")
    token_file = _token_file(config)
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

    ig_user_id = os.environ.get("INSTAGRAM_USER_ID") or saved.get("ig_user_id")
    if not ig_user_id:
        pages = requests.get(f"{base}/me/accounts", params={
            "fields": "name,instagram_business_account{id,username}", "access_token": token,
        }, timeout=60).json().get("data", [])
        linked = [p for p in pages if p.get("instagram_business_account")]
        if not linked:
            raise PublishError("instagram: no Instagram professional account is linked to your Facebook Pages")
        for page in linked:
            ig = page["instagram_business_account"]
            print(f"  Found @{ig.get('username')} (id {ig['id']}) on Page '{page['name']}'")
        ig_user_id = linked[0]["instagram_business_account"]["id"]

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(
        {"ig_user_id": ig_user_id, "access_token": token, "expires_at": expires_at}, indent=2))
    print(f"Saved Instagram token for account {ig_user_id} to {token_file} "
          f"(expires {time.strftime('%Y-%m-%d', time.localtime(expires_at))}).")


class InstagramPublisher(Publisher):
    name = "instagram"

    def publish(self, post: Post, video: Path) -> PublishResult:
        s = self.settings
        version = s.get("api_version", DEFAULT_API_VERSION)
        base = f"https://{s.get('graph_host', 'graph.facebook.com')}/{version}"
        user_id, token = load_credentials(self.config)

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
        resp = self.request("POST", f"{base}/{user_id}/media_publish",
                            data={"creation_id": container_id, "access_token": token})
        if not resp.ok:
            raise PublishError(f"instagram: publish failed: {error_text(resp)}")
        media_id = resp.json()["id"]

        url = None
        r = self.request("GET", f"{base}/{media_id}", params={"fields": "permalink", "access_token": token})
        if r.ok:
            url = r.json().get("permalink")
        return PublishResult(remote_id=media_id, url=url)
