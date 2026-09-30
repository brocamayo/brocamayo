"""YouTube uploads via the YouTube Data API v3 (resumable upload)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from ..queue import Post
from .base import NotLoggedIn, PublishError, PublishResult, Publisher, auth_hint, log

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Read-only, used to show which channel an account is logged in to.
    "https://www.googleapis.com/auth/youtube.readonly",
    # Which Google login was used, so a wrong pick can be explained.
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
# Google may return the granted scopes in a different form; don't treat that as an error.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
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


def _google_email(creds) -> str | None:
    """Email from the login's ID token (display only, so no signature check)."""
    token = getattr(creds, "id_token", None)
    if not token or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email")
    except ValueError:
        return None


def _logout(config, account: str) -> None:
    config.path(config.settings(account, "youtube")["token_file"]).unlink(missing_ok=True)
    channel_file(config, account).unlink(missing_ok=True)


def authorize(config, account: str, want: str | None = None) -> bool:
    """Log in; if `want` (e.g. "@brocmayo") is given, verify the login landed on that channel."""
    from googleapiclient.discovery import build

    print(f"Logging in YouTube for account '{account}'.")
    creds = _credentials(config, account, interactive=True)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    email = _google_email(creds)

    items = youtube.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        _logout(config, account)
        print(f"{email or 'That Google login'} has no YouTube channel. Run it again and pick another login.")
        return False
    got = {"id": items[0]["id"], "title": items[0]["snippet"]["title"],
           "handle": items[0]["snippet"].get("customUrl")}
    print(f"\nSigned in as:  {email or '(unknown Google login)'}")
    print(f"Channel:       {got['title']}" + (f" ({got['handle']})" if got["handle"] else ""))

    if want:
        handle = "@" + want.lstrip("@")
        found = youtube.channels().list(part="snippet", forHandle=handle).execute().get("items", [])
        if not found:
            _logout(config, account)
            print(f"\nCouldn't find a channel with the handle {handle}. Check the spelling and try again.")
            return False
        target = found[0]
        if target["id"] != got["id"]:
            _logout(config, account)
            print(f"\n❌ That's not {handle}. {handle} is the channel named \"{target['snippet']['title']}\".")
            print("Run the same command again. On Google's \"Choose an account\" screen, click the row named")
            print(f"\"{target['snippet']['title']}\" (it may have no email under it, or say \"Brand Account\"),")
            print("instead of an email address. If there's no such row, click the email that manages")
            print(f"{handle} and look for a \"Choose a channel\" screen right after.")
            return False
    else:
        answer = input(f"Is this the right channel for '{account}'? [Y/n] ").strip().lower()
        if answer in ("n", "no"):
            _logout(config, account)
            print(f"Logged out. Run `{auth_hint('youtube', account, config)}` again and pick the other channel.")
            return False

    for other in config.accounts:
        if other != account and saved_channel(config, other).get("id") == got["id"]:
            print(f"Heads up: account '{other}' is logged in to this same channel.")
    channel_file(config, account).write_text(json.dumps(got, indent=2))
    print(f"\n✅ YouTube for '{account}' will post to: {got['title']}")
    return True


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
