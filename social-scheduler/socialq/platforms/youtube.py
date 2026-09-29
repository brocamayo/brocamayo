"""YouTube uploads via the YouTube Data API v3 (resumable upload)."""

from __future__ import annotations

from pathlib import Path

from ..queue import Post
from .base import PublishError, PublishResult, Publisher, log

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK_SIZE = 8 * 1024 * 1024


def _credentials(config, interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    settings = config.youtube
    token_file = config.path(settings.get("token_file", "credentials/youtube_token.json"))
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not interactive:
            raise PublishError("youtube: not authorized yet. Run `python -m socialq auth youtube`.")
        from google_auth_oauthlib.flow import InstalledAppFlow

        secrets = config.path(settings.get("client_secrets", "credentials/youtube_client_secret.json"))
        if not secrets.exists():
            raise PublishError(f"youtube: OAuth client file not found at {secrets} (see README)")
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    return creds


def authorize(config) -> None:
    _credentials(config, interactive=True)


class YouTubePublisher(Publisher):
    name = "youtube"

    def publish(self, post: Post, video: Path) -> PublishResult:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        s = self.settings
        youtube = build("youtube", "v3", credentials=_credentials(self.config), cache_discovery=False)
        body = {
            "snippet": {
                "title": post.youtube_title(),
                "description": post.youtube_description(),
                "tags": post.youtube_tags(),
                "categoryId": str(post.option("youtube", "category_id", s.get("category_id", "22"))),
            },
            "status": {
                "privacyStatus": post.option("youtube", "privacy", s.get("privacy", "public")),
                "selfDeclaredMadeForKids": bool(s.get("made_for_kids", False)),
            },
        }
        media = MediaFileUpload(str(video), chunksize=CHUNK_SIZE, resumable=True, mimetype="video/*")
        try:
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                status, response = request.next_chunk(num_retries=5)
                if status:
                    log.info("youtube: uploaded %d%%", int(status.progress() * 100))
            video_id = response["id"]

            thumb = post.thumbnail_path(self.config)
            if thumb:
                try:
                    youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumb))).execute()
                except HttpError as exc:  # thumbnails need a verified channel; don't fail the post
                    log.warning("youtube: video uploaded but thumbnail failed: %s", exc)
        except HttpError as exc:
            raise PublishError(f"youtube: {exc}") from exc
        return PublishResult(remote_id=video_id, url=f"https://youtu.be/{video_id}")
