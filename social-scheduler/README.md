# socialq: schedule videos to YouTube, Instagram and TikTok

Drop a video in a folder, add one line to a queue, and it gets posted to **YouTube**, **Instagram Reels** and **TikTok** at the time you picked, with the right title, caption and hashtags for each platform. It uses each platform's official API, so there's no browser automation and no risk of an account ban.

```
python -m socialq add videos/nvda-earnings.mp4 --title "NVIDIA earnings in 60s" \
    --caption "What actually mattered this quarter" --tags investing,stocks,nvidia
# Queued 20261005-1700-nvda-earnings for 2026-10-05 17:00 on youtube, instagram, tiktok
```

A scheduled job runs `python -m socialq run` every few minutes. It uploads whatever is due, records what was posted where so nothing ever posts twice, and retries failures.

## What it does

- **One queue, three platforms.** You write one caption. Tags become YouTube tags and are added as `#hashtags` on Instagram and TikTok. You can override the text for any platform.
- **Posting slots.** You set your posting times once (`mon,wed,fri 17:00`), and `add` puts each new video in the next open slot.
- **Safe to re-run.** State lives in `state.json`. If one platform fails, the others still post. A failed upload is retried later (after 30 min, up to 3 attempts by default).
- **Checks before uploading.** `check` catches a missing file, a title over 100 characters, too many hashtags and similar problems before a scheduled post fails.
- **Optional alerts.** It can send a summary of each run to Slack, Discord or Teams.

## Quick start

**Windows, one click:** right-click `setup-windows.ps1` and choose **Run with PowerShell**. It installs everything, creates your config files, and sets up a hidden background task that runs every 10 minutes. After that, use `.\socialq <command>` from the folder. The only steps left are logging in to each platform (below).

**Manual / macOS / Linux:**

```bash
cd social-scheduler
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m socialq init            # creates config.yaml, queue.yaml, .env, videos/, credentials/
# fill in .env and config.yaml (see platform setup below)
python -m socialq auth youtube
python -m socialq auth instagram --token <short-lived token>
python -m socialq auth tiktok

python -m socialq add videos/my-first.mp4 --title "..." --caption "..." --tags a,b
python -m socialq check           # validate everything
python -m socialq run --dry-run   # see what would post right now
```

Then set up the schedule (see [Run it automatically](#run-it-automatically)).

## Commands

| Command | What it does |
|---|---|
| `init [dir]` | Create starter `config.yaml`, `queue.yaml`, `.env` |
| `add VIDEO [--title --caption --tags --at --platforms --id]` | Queue a video. Without `--at` it takes the next free slot |
| `list [-a]` | Show upcoming posts and their status on each platform |
| `check` | Validate the queue: files exist, caption and title limits |
| `run [--dry-run]` | Publish everything that's due, then exit. This is what the scheduled job runs |
| `watch [--every 5]` | Keep running and check every N minutes, instead of using cron |
| `retry POST_ID [platform]` | Clear a failed (or published) status so it's attempted again |
| `auth youtube\|instagram\|tiktok` | Log in to a platform |

Pass `-c path/to/config.yaml` before the command if you're not running from the project folder.

## The queue (`queue.yaml`)

```yaml
- id: nvda-earnings                 # optional, defaults to the file name
  video: nvda-earnings.mp4          # relative to media_dir (videos/)
  publish_at: "2026-10-05 17:00"    # your timezone from config.yaml
  platforms: [youtube, instagram, tiktok]   # optional, defaults to all enabled
  title: "NVIDIA earnings in 60 seconds #Shorts"
  caption: |
    What actually mattered this quarter. Not financial advice.
  tags: [investing, stocks, nvidia]
  thumbnail: nvda-thumb.jpg         # optional, YouTube custom thumbnail
  youtube:                          # optional per-platform overrides
    description: Longer YouTube description...
    privacy: unlisted
  instagram:
    thumb_offset_ms: 2000           # which frame to use as the Reel cover
  tiktok:
    caption: "Shorter TikTok caption #investing"
```

See `queue.example.yaml` for a full example. For vertical Shorts, put `#Shorts` in the title or description.

---

## Platform setup (one time)

Each platform requires you to create a free developer app. Budget about an hour for all three. The TikTok and YouTube review steps take a few days, so start those first.

### YouTube

1. Go to [Google Cloud Console](https://console.cloud.google.com/), create a project, and enable **YouTube Data API v3**.
2. Under **APIs & Services → OAuth consent screen**, set up an External app and add yourself as a test user.
3. Under **Credentials**, choose **Create OAuth client ID → Desktop app**, then download the JSON to `credentials/youtube_client_secret.json`.
4. Run `python -m socialq auth youtube`. A browser opens; sign in with the channel's Google account.

> ⚠️ **Videos from unverified API projects are locked to private.** Google restricts videos uploaded by unaudited API projects to private viewing. To post publicly, complete the [YouTube API audit](https://support.google.com/youtube/contact/yt_api_form). Until then, uploads work but you'll need to flip them to public in YouTube Studio.
> Uploads also use a large share of the default daily API quota (10,000 units), so plan on a handful of uploads per day unless you request more quota.
>
> While the OAuth consent screen is in **Testing** mode, Google expires the login after 7 days. Publish the consent screen (you don't need verification for your own account) to keep the login working.

### Instagram (Reels)

This requires an Instagram **Business or Creator** account linked to a Facebook Page.

1. At [developers.facebook.com](https://developers.facebook.com/), create an app of type **Business** and add the **Instagram** product.
2. Put the App ID and App Secret in `.env` as `META_APP_ID` and `META_APP_SECRET`.
3. In [Graph API Explorer](https://developers.facebook.com/tools/explorer/), select your app and generate a user token with `instagram_basic`, `instagram_content_publish`, `pages_show_list` and `pages_read_engagement`.
4. Run `python -m socialq auth instagram --token <that token>`. This swaps it for a 60-day token, finds your Instagram account ID and saves both.
5. Before the 60 days are up, run `python -m socialq auth instagram --refresh`. The tool warns you in the log when the token is 7 days from expiring. For a token that never expires, create a **System User** in Meta Business Settings and put its token in `.env` as `INSTAGRAM_ACCESS_TOKEN`, with `INSTAGRAM_USER_ID`.

Videos upload directly from your computer, so they don't need to be hosted anywhere. Instagram limits API publishing to about 50 posts per account per 24 hours.

### TikTok

1. At [developers.tiktok.com](https://developers.tiktok.com/), create an app, then add **Login Kit** and **Content Posting API**. Turn on **Direct Post** if you want fully automatic posting.
2. Add a redirect URI. Any HTTPS page you control works, even one that shows an error, because you only need the address it lands on. Put the same URI in `config.yaml` under `tiktok.redirect_uri`.
3. Put the Client Key and Secret in `.env` as `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`.
4. Run `python -m socialq auth tiktok`, open the link, approve access, and paste back the address you land on. After that, the tool refreshes the login automatically for up to a year.

> ⚠️ **TikTok has two modes** (`tiktok.mode` in config.yaml):
> - **`inbox` (default).** The video arrives in your TikTok app as a draft notification, and you tap **Post**, which takes about 10 seconds on your phone. This works right away.
> - **`direct`.** Posts go out fully automatically, but TikTok must [audit your app](https://developers.tiktok.com/doc/content-sharing-guidelines) first. Until the audit passes, direct posts can only be private (`SELF_ONLY`). Switch to `direct` once you're approved.

---

## Run it automatically

`run` is safe to call as often as you like, because a lock file stops overlapping runs. Every 5 to 15 minutes works well. The computer needs to be on and awake at posting time. If it isn't, the post goes out on the next run after it wakes up.

**Windows (Task Scheduler)**

```powershell
schtasks /Create /TN "socialq" /SC MINUTE /MO 10 /TR "cmd /c cd /d C:\path\to\social-scheduler && .venv\Scripts\python.exe -m socialq run"
```

To stop Windows from skipping runs while on battery, open the task in Task Scheduler and, under *Conditions*, uncheck "Start the task only if the computer is on AC power".

**macOS / Linux (cron)**: run `crontab -e` and add:

```
*/10 * * * * cd /path/to/social-scheduler && .venv/bin/python -m socialq run >> cron.log 2>&1
```

**Or keep it running in a terminal:** `python -m socialq watch --every 5`

For always-on posting without leaving your computer on, run the same cron line on a small cloud VM or a Raspberry Pi, with your `videos/` folder synced through Dropbox, OneDrive or Google Drive.

Logs go to `socialq.log`. To get a summary after each run, set `NOTIFY_WEBHOOK_URL` in `.env` to a Slack, Discord or Teams incoming webhook.

## Files

```
social-scheduler/
├── socialq/
│   ├── cli.py            commands
│   ├── scheduler.py      finds due posts, publishes, records results
│   ├── queue.py          queue parsing, per-platform captions, validation
│   ├── slots.py          "mon,wed,fri 17:00" posting slots
│   ├── state.py          state.json + run lock
│   └── platforms/        youtube.py, instagram.py, tiktok.py
├── tests/                pytest suite (no network needed)
├── config.example.yaml
├── queue.example.yaml
└── .env.example
```

Your `.env`, `config.yaml`, `queue.yaml`, `state.json`, `credentials/` and `videos/` are git-ignored, so tokens and videos never get committed.

Run the tests with `pip install -r requirements-dev.txt && python -m pytest tests`.

## Ideas for later

- Generate captions, titles and hashtags from the video transcript with the Claude API.
- Use YouTube's own scheduling (upload early as private with `publishAt`) so the computer doesn't need to be on at posting time.
- Add a watch folder so dropping a file into `videos/inbox/` queues it automatically.
- Support Facebook Reels, X and LinkedIn.
