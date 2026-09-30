# socialq: schedule videos to YouTube, Instagram, TikTok and X

Drop a video in a folder, add one line to a queue, and it gets posted to **YouTube**, **Instagram Reels**, **TikTok** and **X** on **as many accounts as you have**, at the time you picked, with the right title, caption and hashtags for each platform. It uses each platform's official API, so there's no browser automation and no risk of an account ban.

```
python -m socialq add videos/nvda-earnings.mp4 --title "NVIDIA earnings in 60s" \
    --caption "What actually mattered this quarter" --tags investing,stocks,nvidia
# Queued 20261005-1700-nvda-earnings for 2026-10-05 17:00 -> main/youtube, main/instagram,
#   main/tiktok, main/x, second/youtube, second/instagram, second/tiktok, second/x
```

A scheduled job runs `python -m socialq run` every few minutes. It uploads whatever is due, records what was posted where so nothing ever posts twice, and retries failures.

## What it does

- **One queue, four platforms, several accounts.** You write one caption. Tags become YouTube tags and are added as `#hashtags` everywhere else. You can override the text per platform and per account.
- **Staggered accounts.** Give an account a `delay_minutes` and it posts that long after the others, so two accounts don't publish identical videos at the same moment.
- **Posting slots.** You set your posting times once (`mon,wed,fri 17:00`), and `add` puts each new video in the next open slot.
- **Safe to re-run.** State lives in `state.json`. If one account or platform fails, the rest still post. A failed upload is retried later (after 30 min, up to 3 attempts by default). A platform you haven't logged in to yet just waits, without using up retries.
- **Checks before uploading.** `check` catches a missing file, a title over 100 characters, an X post over 280 characters and similar problems before a scheduled post fails.
- **Optional alerts.** It can send a summary of each run to Slack, Discord or Teams.

## Quick start

**Windows, one click:** right-click `setup-windows.ps1` and choose **Run with PowerShell**. It installs everything, creates your config files, and sets up a hidden background task that runs every 10 minutes. After that, use `.\socialq <command>` from the folder. The only steps left are logging in to each platform (below).

**Mac, one command:** put the folder in your home folder (for example `~/social-scheduler`, not Desktop or Documents, which macOS blocks background jobs from reading), then run `bash setup-mac.sh` in Terminal from inside it. It does the same as the Windows script, using a launchd job that runs every 10 minutes. After that, use `./sq <command>`, for example `./sq accounts`.

**Manual / Linux:**

```bash
cd social-scheduler
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m socialq init            # creates config.yaml, queue.yaml, .env, videos/, credentials/
# fill in .env and config.yaml (see platform setup below)
python -m socialq accounts        # shows every login you still need, with the exact command

python -m socialq add videos/my-first.mp4 --title "..." --caption "..." --tags a,b
python -m socialq check           # validate everything
python -m socialq run --dry-run   # see what would post right now
```

Then set up the schedule (see [Run it automatically](#run-it-automatically)).

## Accounts

Accounts are defined in `config.yaml`. The example has two, `main` and `second`, and you can rename them or add more:

```yaml
accounts:
  main:
    platforms: [youtube, instagram, tiktok, x]
  second:
    platforms: [youtube, instagram, tiktok, x]
    delay_minutes: 60          # posts an hour after main
    tiktok: {mode: direct}     # any platform setting can be overridden per account
```

Each account logs in to each platform separately:

```bash
python -m socialq auth youtube --account main
python -m socialq auth youtube --account second
# ...same for instagram, tiktok, x
python -m socialq accounts     # check what's logged in
```

The **developer app** for each platform (the keys in `.env` and the YouTube client file) is set up **once** and shared by all accounts.

> 💡 When logging in the second account on TikTok or X, open the login link in a **private/incognito window**, so you don't accidentally approve it with the first account.

## Commands

| Command | What it does |
|---|---|
| `init [dir]` | Create starter `config.yaml`, `queue.yaml`, `.env` |
| `accounts` | Show each account and which platforms are logged in |
| `auth PLATFORM --account NAME` | Log in one account to `youtube`, `instagram`, `tiktok` or `x` |
| `add VIDEO [--title --caption --tags --at --accounts --platforms --id]` | Queue a video. Without `--at` it takes the next free slot; without `--accounts` it goes to every account |
| `list [-a]` | Show upcoming posts and their status on each account/platform |
| `check` | Validate the queue: files exist, caption and title limits |
| `run [--dry-run]` | Publish everything that's due, then exit. This is what the scheduled job runs |
| `watch [--every 5]` | Keep running and check every N minutes, instead of using cron |
| `retry POST_ID [where]` | Clear a status so it's attempted again. `where` can be `youtube`, `second`, or `second/youtube` |

Pass `-c path/to/config.yaml` before the command if you're not running from the project folder.

## The queue (`queue.yaml`)

```yaml
- id: nvda-earnings                 # optional, defaults to the file name
  video: nvda-earnings.mp4          # relative to media_dir (videos/)
  publish_at: "2026-10-05 17:00"    # your timezone from config.yaml
  accounts: [main, second]          # optional, defaults to every account
  platforms: [youtube, instagram, tiktok, x]   # optional, defaults to each account's platforms
  title: "NVIDIA earnings in 60 seconds #Shorts"
  caption: |
    What actually mattered this quarter. Not financial advice.
  tags: [investing, stocks, nvidia]
  thumbnail: nvda-thumb.jpg         # optional, YouTube custom thumbnail
  youtube:                          # optional per-platform overrides
    description: Longer YouTube description...
  instagram:
    thumb_offset_ms: 2000           # which frame to use as the Reel cover
  x:
    caption: "Shorter text for X (280 chars) #NVDA"
  per_account:                      # optional: different text for one account
    second:
      title: "3 things from NVIDIA's earnings you missed #Shorts"
      caption: "The 3 numbers that actually matter."
      tiktok: {caption: "..."}
```

See `queue.example.yaml` for a full example. For vertical Shorts, put `#Shorts` in the title or description.

> ⚠️ Posting the exact same video and caption to two accounts on the same platform can be treated as duplicate or spam content, which limits reach. Use `per_account` text and `delay_minutes` to keep the accounts distinct.

---

## Platform setup (one time)

Each platform requires you to create a developer app. Budget about an hour for all four. The TikTok and YouTube review steps take a few days, so start those first.

Several steps ask for a **redirect URI** (a web address the login sends you back to). Use `https://brocamayo.github.io/`. You only need the address the browser lands on, not the page itself.

### YouTube

1. Go to [Google Cloud Console](https://console.cloud.google.com/), create a project, and enable **YouTube Data API v3**.
2. Under **Google Auth Platform**, set up the consent screen as an External app. Under **Audience**, add **every Google account that owns one of your channels** as a test user.
3. Under **Clients**, choose **Create client → Desktop app**, then download the JSON to `credentials/youtube_client_secret.json`.
4. Run `python -m socialq auth youtube --account main`. A browser opens; sign in and pick the channel for that account. Repeat with `--account second` and pick the other channel.

> ⚠️ **Videos from unverified API projects are locked to private.** Google restricts videos uploaded by unaudited API projects to private viewing. To post publicly, complete the [YouTube API audit](https://support.google.com/youtube/contact/yt_api_form). Until then, uploads work but you'll need to flip them to public in YouTube Studio.
> Uploads also use a large share of the default daily API quota (10,000 units), which is shared by all your channels, so plan on a handful of uploads per day unless you request more quota.
>
> While the consent screen is in **Testing** mode, Google expires the login after 7 days. Publish the app under **Audience → Publish app** (you don't need verification for your own accounts) to keep the login working.

### Instagram (Reels)

Each Instagram account must be a **Business or Creator** account linked to a Facebook Page that you manage.

1. At [developers.facebook.com](https://developers.facebook.com/), create an app of type **Business** and add the **Instagram** product.
2. Put the App ID and App Secret in `.env` as `META_APP_ID` and `META_APP_SECRET`.
3. In [Graph API Explorer](https://developers.facebook.com/tools/explorer/), select your app and generate a user token with `instagram_basic`, `instagram_content_publish`, `pages_show_list` and `pages_read_engagement`.
4. Run `python -m socialq auth instagram --account main --token <that token>`. It swaps the token for a 60-day one, lists your linked Instagram accounts and asks which one this is. Repeat for `--account second` (a fresh token from step 3 works) and pick the other account. You can skip the question with `--ig-username @handle`.
5. Before the 60 days are up, run `python -m socialq auth instagram --account main --refresh` (and the same for `second`). `socialq accounts` shows the expiry dates, and the log warns you 7 days ahead.

Videos upload directly from your computer, so they don't need to be hosted anywhere. Instagram limits API publishing to about 50 posts per account per 24 hours.

### TikTok

1. At [developers.tiktok.com](https://developers.tiktok.com/), create an app, then add **Login Kit** and **Content Posting API**. Turn on **Direct Post** if you want fully automatic posting.
2. Add the redirect URI `https://brocamayo.github.io/` (it's already in `config.yaml` under `tiktok.redirect_uri`).
3. Put the Client Key and Secret in `.env` as `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`.
4. Run `python -m socialq auth tiktok --account main`, open the link, approve access, and paste back the address you land on. Do the same for `--account second` in a private/incognito window. After that, the tool refreshes each login automatically for up to a year.

> ⚠️ **TikTok has two modes** (`tiktok.mode` in config.yaml, can differ per account):
> - **`inbox` (default).** The video arrives in your TikTok app as a draft notification, and you tap **Post**, which takes about 10 seconds on your phone. This works right away.
> - **`direct`.** Posts go out fully automatically, but TikTok must [audit your app](https://developers.tiktok.com/doc/content-sharing-guidelines) first. Until the audit passes, direct posts can only be private (`SELF_ONLY`). Switch to `direct` once you're approved.

### X

> 💳 **The X API is pay-per-use.** There's no free tier for new developers. You buy credits in the developer console, and each post costs a small fee (about 1.5¢ at the time of writing, more if it contains a link). Check current pricing in the console before you start.

1. At [console.x.com](https://console.x.com/) (the X developer portal), create a project and an app, and add credits.
2. In the app's **User authentication settings**, turn on **OAuth 2.0**. Set app permissions to **Read and write**, type of app to **Web App, Automated App or Bot**, and the callback URI to `https://brocamayo.github.io/` (already in `config.yaml` under `x.redirect_uri`).
3. Copy the **OAuth 2.0 Client ID and Client Secret** (not the API key and secret) into `.env` as `X_CLIENT_ID` and `X_CLIENT_SECRET`.
4. Run `python -m socialq auth x --account main`, open the link, approve access, and paste back the address you land on. Repeat for `--account second` in a private/incognito window. The login refreshes itself automatically.

X counts every link as 23 characters, and posts are capped at 280 characters unless the account has X Premium (then set `max_chars` under that account's `x:` settings). Standard accounts can post videos up to about 2 minutes 20 seconds long.

---

## Run it automatically

`run` is safe to call as often as you like, because a lock file stops overlapping runs. Every 5 to 15 minutes works well. The computer needs to be on and awake at posting time. If it isn't, the post goes out on the next run after it wakes up.

**Windows:** `setup-windows.ps1` already sets this up. To do it by hand:

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
│   ├── config.py         config.yaml + accounts
│   ├── scheduler.py      finds due posts, publishes, records results
│   ├── queue.py          queue parsing, per-platform/per-account text, validation
│   ├── slots.py          "mon,wed,fri 17:00" posting slots
│   ├── state.py          state.json + run lock
│   └── platforms/        youtube.py, instagram.py, tiktok.py, x.py
├── tests/                pytest suite (no network needed)
├── setup-windows.ps1     one-click Windows install + background task
├── config.example.yaml
├── queue.example.yaml
└── .env.example
```

Logins are saved per account under `credentials/<account>/`. Your `.env`, `config.yaml`, `queue.yaml`, `state.json`, `credentials/` and `videos/` are git-ignored, so tokens and videos never get committed.

Run the tests with `pip install -r requirements-dev.txt && python -m pytest tests`.

## Ideas for later

- Generate captions, titles and hashtags from the video transcript with the Claude API, with different wording for each account.
- Use YouTube's own scheduling (upload early as private with `publishAt`) so the computer doesn't need to be on at posting time.
- Add a watch folder so dropping a file into `videos/inbox/` queues it automatically.
- Support Facebook Reels and LinkedIn.
