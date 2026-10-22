"""Facebook Pages via the Graph API: short vertical videos as Reels, everything else as a video post.

Meta only lets apps post to Pages, not personal profiles. Logging in swaps a Graph API
Explorer token for a Page token that doesn't expire.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from ..media import probe
from ..queue import Post
from .base import NotLoggedIn, PublishError, PublishResult, Publisher, auth_hint, error_text, log, poll

DEFAULT_API_VERSION = "v25.0"
GRAPH = "https://graph.facebook.com"
GRAPH_VIDEO = "https://graph-video.facebook.com"   # host for regular (non-Reel) video uploads
REEL_UPLOAD = "https://rupload.facebook.com/video-upload"
REEL_MAX_S = 90
UPLOAD_ATTEMPTS = 5
PERMISSIONS = "pages_show_list,pages_read_engagement,pages_manage_posts"


def _token_path(config, account: str) -> Path:
    return config.path(config.settings(account, "facebook")["token_file"])


def load_page(config, account: str) -> dict:
    token_file = _token_path(config, account)
    if not token_file.exists():
        raise NotLoggedIn(f"facebook ({account}): not logged in. "
                          f"Run `{auth_hint('facebook', account, config)} --token <token>` (see README)")
    saved = json.loads(token_file.read_text())
    if not saved.get("page_id") or not saved.get("access_token"):
        raise PublishError(f"facebook ({account}): {token_file} is missing page_id or access_token")
    return saved


def _pick_page(pages: list[dict], wanted: str | None) -> dict:
    if not pages:
        raise PublishError("facebook: that login can't manage any Pages. When you generate the token, "
                           "tick your Pages and grant " + PERMISSIONS)
    if wanted:
        key = wanted.strip().lstrip("@").lower()
        for page in pages:
            if key in (page["id"], page.get("name", "").lower()):
                return page
        raise PublishError(f"facebook: no Page called {wanted!r} (found: "
                           f"{', '.join(p.get('name', p['id']) for p in pages)})")
    if len(pages) == 1:
        return pages[0]
    print("Which Facebook Page is this account?")
    for i, page in enumerate(pages, 1):
        print(f"  {i}. {page.get('name')}")
    choice = input("Number: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(pages):
        raise PublishError("facebook: no Page picked")
    return pages[int(choice) - 1]


def authorize(config, account: str, user_token: str | None, page: str | None = None) -> None:
    """Swap a Graph API Explorer user token for a Page token and save it."""
    if not user_token:
        raise PublishError("facebook: pass --token <token from Graph API Explorer> (see README)")
    app_id, app_secret = os.environ.get("META_APP_ID"), os.environ.get("META_APP_SECRET")
    if not app_id or not app_secret:
        raise PublishError("facebook: put META_APP_ID and META_APP_SECRET in .env first "
                           "(Meta app dashboard -> App settings -> Basic)")
    version = config.settings(account, "facebook").get("api_version", DEFAULT_API_VERSION)
    base = f"{GRAPH}/{version}"

    # A long-lived user token makes Page tokens that never expire.
    resp = requests.get(f"{base}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": user_token}, timeout=60)
    if not resp.ok:
        raise PublishError(f"facebook: that token didn't work: {error_text(resp)}")
    long_lived = resp.json()["access_token"]

    resp = requests.get(f"{base}/me/accounts", params={
        "fields": "id,name,access_token,tasks", "limit": 100, "access_token": long_lived}, timeout=60)
    if not resp.ok:
        raise PublishError(f"facebook: couldn't list your Pages: {error_text(resp)}")
    chosen = _pick_page(resp.json().get("data", []), page)
    tasks = chosen.get("tasks") or []
    if tasks and "CREATE_CONTENT" not in tasks:
        raise PublishError(f"facebook: you can't post on {chosen.get('name')} (your role there is too limited)")

    token_file = _token_path(config, account)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps({
        "page_id": chosen["id"], "page_name": chosen.get("name"), "access_token": chosen["access_token"],
    }, indent=2))
    print(f"Facebook logged in for '{account}' as the Page {chosen.get('name')}. This login doesn't expire.")
    for other in config.accounts:
        if other == account:
            continue
        other_file = _token_path(config, other)
        try:
            if other_file.exists() and json.loads(other_file.read_text()).get("page_id") == chosen["id"]:
                print(f"⚠️  '{other}' posts to this same Page. Use --page to pick a different one.")
        except (ValueError, OSError):
            pass


def as_reel(post: Post, video: Path, settings: dict) -> bool:
    """Reel for short vertical videos, a regular video post for the rest (or as set with `as:`)."""
    kind = str(post.option("facebook", "as", settings.get("as", "auto"))).lower()
    if kind in ("reel", "video"):
        return kind == "reel"
    info = probe(video)
    if info is None:
        log.info("facebook: couldn't read the video's size, posting it as a regular video")
        return False
    return info.vertical and info.duration_s <= float(settings.get("reel_max_seconds", REEL_MAX_S))


class FacebookPublisher(Publisher):
    name = "facebook"

    def publish(self, post: Post, video: Path) -> PublishResult:
        saved = load_page(self.config, self.account)
        self.page_id, self.token = saved["page_id"], saved["access_token"]
        self.version = self.settings.get("api_version", DEFAULT_API_VERSION)
        log.info("facebook (%s): posting to the Page %s", self.account, saved.get("page_name") or self.page_id)
        if as_reel(post, video, self.settings):
            return self._reel(post, video)
        return self._video(post, video)

    # --- Reels -----------------------------------------------------------------

    def _reel(self, post: Post, video: Path) -> PublishResult:
        base = f"{GRAPH}/{self.version}"
        resp = self.request("POST", f"{base}/{self.page_id}/video_reels",
                            data={"upload_phase": "start", "access_token": self.token})
        if not resp.ok:
            raise PublishError(f"facebook: starting the Reel failed: {error_text(resp)}")
        video_id = resp.json()["video_id"]
        upload_url = resp.json().get("upload_url") or f"{REEL_UPLOAD}/{self.version}/{video_id}"

        self._upload_reel(upload_url, video, video_id)

        resp = self.request("POST", f"{base}/{self.page_id}/video_reels", creates=True, data={
            "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
            "description": post.social_caption("facebook"), "access_token": self.token})
        if not resp.ok or not resp.json().get("success", True):
            raise PublishError(f"facebook: publishing the Reel failed: {error_text(resp)}")
        log.info("facebook: Reel sent, waiting for Facebook to process it")
        self._wait_until_live(video_id)
        return PublishResult(remote_id=video_id, url=self._permalink(video_id, f"/reel/{video_id}"))

    def _status(self, video_id: str) -> dict:
        r = self.request("GET", f"{GRAPH}/{self.version}/{video_id}",
                         params={"fields": "status", "access_token": self.token})
        if not r.ok:
            raise PublishError(f"facebook: status check failed: {error_text(r)}")
        return r.json().get("status") or {}

    def _upload_reel(self, upload_url: str, video: Path, video_id: str) -> None:
        """Send the file to rupload, picking up where it left off if the connection drops."""
        size = video.stat().st_size
        offset = 0
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            log.info("facebook: uploading %.1f MB%s", (size - offset) / 1e6,
                     f" (resuming at {offset / size:.0%})" if offset else "")
            with video.open("rb") as fh:
                fh.seek(offset)
                try:
                    resp = self.session.request("POST", upload_url, data=fh, timeout=900, headers={
                        "Authorization": f"OAuth {self.token}", "offset": str(offset), "file_size": str(size)})
                except (requests.ConnectionError, requests.Timeout) as exc:
                    resp, problem = None, str(exc)
            if resp is not None:
                try:
                    if resp.ok and resp.json().get("success", True):
                        return
                except ValueError:
                    pass
                problem = error_text(resp)
                if resp.status_code < 500 and "Offset" not in problem:
                    raise PublishError(f"facebook: upload failed: {problem}")
            if attempt == UPLOAD_ATTEMPTS:
                raise PublishError(f"facebook: upload kept failing ({problem[:200]})")
            sent = (self._status(video_id).get("uploading_phase") or {}).get("bytes_transferred")
            offset = int(sent or 0)
            log.warning("facebook: upload interrupted (%s), resuming", problem[:120])
            time.sleep(2 ** attempt)

    def _wait_until_live(self, video_id: str) -> None:
        """Watch processing so errors show up; the post is already live-bound, so a slow
        Facebook only logs a note instead of failing (a retry would post it twice)."""
        def done() -> bool:
            status = self._status(video_id)
            processing = status.get("processing_phase") or {}
            if status.get("video_status") == "error" or processing.get("status") == "error":
                raise PublishError(f"facebook: processing failed: {json.dumps(processing.get('errors') or status)[:300]}")
            publishing = status.get("publishing_phase") or {}
            return publishing.get("status") == "complete" or status.get("video_status") == "ready"

        try:
            poll(done, timeout_s=float(self.settings.get("processing_timeout_s", 600)), interval_s=10,
                 what="Facebook to process the video")
        except PublishError as exc:
            if "Timed out" not in str(exc):
                raise
            log.warning("facebook: still processing after %ss; it will go live when Facebook finishes",
                        self.settings.get("processing_timeout_s", 600))

    # --- regular video posts -----------------------------------------------------

    def _video(self, post: Post, video: Path) -> PublishResult:
        url = f"{GRAPH_VIDEO}/{self.version}/{self.page_id}/videos"
        size = video.stat().st_size
        resp = self.request("POST", url, data={
            "upload_phase": "start", "file_size": str(size), "access_token": self.token})
        if not resp.ok:
            raise PublishError(f"facebook: starting the upload failed: {error_text(resp)}")
        body = resp.json()
        session_id, video_id = body["upload_session_id"], body["video_id"]
        start, end = int(body["start_offset"]), int(body["end_offset"])

        log.info("facebook: uploading %.1f MB", size / 1e6)
        last_logged = 0
        with video.open("rb") as fh:
            while start < end:
                fh.seek(start)
                chunk = fh.read(end - start)
                resp = self.request("POST", url, timeout=300, data={
                    "upload_phase": "transfer", "upload_session_id": session_id,
                    "start_offset": str(start), "access_token": self.token,
                }, files={"video_file_chunk": ("chunk", chunk, "application/octet-stream")})
                if not resp.ok:
                    raise PublishError(f"facebook: upload failed at {start / size:.0%}: {error_text(resp)}")
                start, end = int(resp.json()["start_offset"]), int(resp.json()["end_offset"])
                if start * 4 // size > last_logged:
                    last_logged = start * 4 // size
                    log.info("facebook: uploaded %d%%", start * 100 // size)

        resp = self.request("POST", url, creates=True, data={
            "upload_phase": "finish", "upload_session_id": session_id, "access_token": self.token,
            "title": post.youtube_title(), "description": post.social_caption("facebook")})
        if not resp.ok or not resp.json().get("success", True):
            raise PublishError(f"facebook: publishing the video failed: {error_text(resp)}")
        return PublishResult(remote_id=video_id,
                             url=self._permalink(video_id, f"/{self.page_id}/videos/{video_id}"))

    def _permalink(self, video_id: str, fallback: str) -> str:
        r = self.request("GET", f"{GRAPH}/{self.version}/{video_id}",
                         params={"fields": "permalink_url", "access_token": self.token})
        link = (r.json().get("permalink_url") if r.ok else None) or fallback
        return link if link.startswith("http") else f"https://www.facebook.com{link}"
