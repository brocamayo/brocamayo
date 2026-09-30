"""YouTube uploads via the YouTube Data API v3 (resumable upload)."""

from __future__ import annotations

import json
from pathlib import Path

from ..queue import Post
from .base import NotLoggedIn, PublishError, PublishResult, Publisher, auth_hint, log

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Read-only, used to show which channel an account is logged in to.
    "https://www.googleapis.com/auth/youtube.readonly",
]
CHUNK_SIZE = 8 * 1024 * 1024


def _credentials(config, account: str, interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    settings = config.settings(account, "youtube")
    token_file = config.path(settings["token_file"])
    creds = None
    if token_file.exists():
        # Use the scopes saved with the token: older logins may not have youtube.readonly.
        creds = Credentials.from_authorized_user_file(str(token_file))
    if creds and creds.expired and creds.refresh_token and not interactive:
        creds.refresh(Request())
    if interactive or not creds or not creds.valid:
        if not interactive:
            raise NotLoggedIn(f"youtube ({account}): not logged in. Run `{auth_hint('youtube', account, config)}`.")
        from google_auth_oauthlib.flow import InstalledAppFlow

        secrets = config.path(settings.get("client_secrets", "credentials/youtube_client_secret.json"))
        if not secrets.exists():
            raise PublishError(f"youtube: OAuth client file not found at {secrets} (see README)")
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
        # select_account makes Google ask which account/channel to use every time.
        creds = flow.run_local_server(port=0, prompt="consent select_account", access_type="offline")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    return creds


def channel_file(config, account: str) -> Path:
    return config.path(config.settings(account, "youtube")["token_file"]).with_name("youtube_channel.json")


def saved_channel(config, account: str) -> dict:
    path = channel_file(config, account)
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {}


def authorize(config, account: str) -> None:
    from googleapiclient.discovery import build

    print(f"Logging in YouTube for account '{account}'.")
    print("In the browser: pick the Google account, then on the next screen pick the CHANNEL")
    print("for this account (channels you manage are listed separately, e.g. as Brand Accounts).\n")
    creds = _credentials(config, account, interactive=True)

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = youtube.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        print(f"YouTube logged in for '{account}', but that Google account has no channel.")
        return
    channel = {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}
    print(f"\nLogged in to channel: {channel['title']}  (https://youtube.com/channel/{channel['id']})")

    for other in config.accounts:
        if other != account and saved_channel(config, other).get("id") == channel["id"]:
            print(f"Heads up: account '{other}' is logged in to this same channel.")

    answer = input(f"Is this the right channel for '{account}'? [Y/n] ").strip().lower()
    if answer in ("n", "no"):
        config.path(config.settings(account, "youtube")["token_file"]).unlink(missing_ok=True)
        channel_file(config, account).unlink(missing_ok=True)
        print(f"Logged out. Run `{auth_hint('youtube', account, config)}` again and pick the other channel.")
        return
    channel_file(config, account).write_text(json.dumps(channel, indent=2))
    print(f"YouTube logged in for '{account}' -> {channel['title']}.")


class YouTubePublisher(Publisher):
    name = "youtube"

    def publish(self, post: Post, video: Path) -> PublishResult:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        s = self.settings
        channel = saved_channel(self.config, self.account).get("title")
        if channel:
            log.info("youtube (%s): uploading to channel %s", self.account, channel)
        youtube = build("youtube", "v3", credentials=_credentials(self.config, self.account),
                        cache_discovery=False)
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
