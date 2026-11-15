# socialq: Broc's video poster

This folder is **socialq**, a CLI that posts Broc's videos to YouTube, TikTok, Instagram, Facebook and X.
On the Mac it lives in `~/social-scheduler`. Run it as `./sq <command>`. A launchd job runs `./sq run`
every 10 minutes in the background, so anything queued with a past time goes out on its own.

Usually Broc pastes a **packaging block**: a file path, title, description, pinned comment, captions
per platform, and sometimes Spanish. Your job is to check it, queue it with `./sq add`, run it, and tell
Broc what's left to do by hand. Sending the package is the go-ahead to post. Stop and ask only when
something below says to.

## Accounts and routing

| account | YouTube | TikTok | Instagram | Facebook | X |
|---|---|---|---|---|---|
| `brocmayo` (Broc) | channel "Broc" | Broc | off | off | on |
| `snowballer` (posts 60 min later) | channel "Snowballer App" | snowballerapp | @snowballerapp | Page "Snowballer" | n/a |

Which kind of video goes where:

- **Snowballer short** (about the app, debt payoff, budgeting): **all socials**. That's two `add`s:
  1. YouTube on both channels: `--accounts brocmayo snowballer --platforms youtube`
  2. A `-social` entry: `--accounts snowballer --platforms tiktok instagram facebook`

  Then run `./sq spread instagram facebook >/dev/null`, which spaces IG/FB out to 3 a day at 09:00/13:00/18:00.
- **"skip brocmayo yt"** means YouTube on the Snowballer channel only (`--accounts snowballer`).
- **Not about Snowballer** (AI money, AI subscriptions, Nvidia, Claude/ChatGPT/Grok): **never on Snowballer accounts.**
  - Long videos ("Before Work", "Fine Print", AI Money Check): Broc's YouTube only.
  - Shorts: Broc's YouTube and TikTok: `--accounts brocmayo --platforms youtube tiktok`.
  - Ignore any FB/IG captions in the package, because Broc has no FB/IG.
- **X**: Broc hasn't decided what goes there. Don't add `x` unless asked. For a short that links to a
  long video, it's fine to *offer* X, since links are clickable there.

## Checks before queuing

1. **The file exists.** If it's missing, look for it with
   `find ~/Desktop ~/Downloads ~/Movies ~/Documents -iname '*part-of-name*'` and say so.
   Don't queue a missing file.
2. **Fact-check the numbers and claims.** Redo the math (e.g. +$15/mo = +$180/yr). Check prices and dates
   against the sources, and search the web for news claims. Check model pricing with the claude-api skill.
   Tell Broc what you checked. If something is wrong, stop and ask before posting.
3. **YouTube chapters.** The first chapter must be `0:00`, and every chapter must be **at least 10 seconds**,
   including the last one (the video must run 10s or more past the last timestamp). If any chapter is
   under 10s, YouTube drops *all* of them. Merge a short title card into the next chapter.
4. **Time-sensitive wording.** Words like "today", "tomorrow (Nov 13)" or "until Monday" mean it must post on
   the right day. Point that out, and offer the fix if it would post late.
5. **Placeholders.** `[LONG-FORM LINK]` comes from the posted long video (see below). For any other
   `[SOMETHING LINK]`, find the real link, and tell Broc which one you used so he can confirm it.
6. **Duplicate ids.** `./sq add` refuses an id that already exists, so pick a new, descriptive one.
7. **Snowballer disclosure.** Broc built the app. If "I built it" is missing from the TikTok/IG captions,
   mention it once (FTC). Earlier shorts linked `https://snowballerapp.com/yt`, which tracks YouTube
   sign-ups; mention it if a YouTube description has no link.

Keep the description **word for word**. Only rejoin lines that the paste split up, with blank lines
between paragraphs. Never rewrite Broc's copy unless a check above fails.

## Commands

Post now = `--at "2026-10-01 09:00"` (any past time). A future `--at 'YYYY-MM-DD HH:MM'` waits in
the queue. Use heredocs with a quoted `'EOF'` so `$` and quotes survive. Put titles with `$` in
single quotes.

**Broc YouTube long video:**
```
cd ~/social-scheduler && V=~/Downloads/NAME.mp4
CAPTION=$(cat <<'EOF'
...description...
EOF
)
ESDESC=$(cat <<'EOF'
...Spanish description, if given...
EOF
)
./sq add "$V" --id some-id --title 'Title with $' --caption "$CAPTION" --captions ~/Downloads/NAME.srt \
  --translation es 'Título' "$ESDESC" --comment "Pinned comment" \
  --accounts brocmayo --platforms youtube --at "2026-10-01 09:00" && caffeinate -dims ./sq run
```
Leave out `--captions` if there's no .srt, and `--translation` if there's no Spanish.

**Short that links to its long video.** The long video's URL is in `state.json` under
`posts.<long-id>["brocmayo/youtube"].url`. If it's empty, the long video isn't up yet: don't queue the short.
```
LINK=$(python3 -c "import json;print(json.load(open('state.json'))['posts'].get('LONG-ID',{}).get('brocmayo/youtube',{}).get('url') or '')")
```
Put `$LINK` in place of `[LONG-FORM LINK]` in the description and the comment. Pass the TikTok
caption with `--tiktok-caption`.

**Snowballer short:**
```
./sq add "$V" --id snowballer-x --title "$TITLE" --caption "$YTCAP" --captions FILE.srt --comment '...' \
  --accounts brocmayo snowballer --platforms youtube --at "2026-10-01 09:00" \
&& ./sq add "$V" --id snowballer-x-social --title "$TITLE" --caption "$YTCAP" --tiktok-caption "$TTCAP" \
  --ig-caption "$IGCAP" --fb-caption "$FBCAP" --accounts snowballer --platforms tiktok instagram facebook \
  --at "2026-10-01 09:00" \
&& ./sq spread instagram facebook >/dev/null && caffeinate -dims ./sq run
```

**Reschedule posts that are already queued** (there's no CLI command for it):
```
.venv/bin/python -c "
from socialq.config import load_config
from socialq.queue import set_publish_times
print(set_publish_times(load_config('config.yaml'), {'post-id': '2026-11-20 08:00'}), 'moved')"
```
To post queued items right now, give them a time a minute or two ago, in the order they should go,
then `./sq run`.

**Other commands:**
- `./sq list`: what's queued.
- `./sq accounts`: logins.
- `./sq remove ID`: take a post out of the queue.
- `./sq retry failed [platform]`: re-queue failed uploads.
- `./sq check`: validate the queue.

**"Another run is in progress"** means the background job is uploading. That's fine: it will post the new
item too. To make the new item go first:
`pkill -f "socialq run"; sleep 2; rm -f .socialq.lock && caffeinate -dims ./sq run`

## Spacing

- Avoid more than about 3 posts a day on Broc's YouTube.
- Don't post a long video's shorts in the same hour as it. Spread them over the next days, and put
  the most timely one first.
- Payday-themed shorts should go out on a payday morning (Friday, or the 1st/15th).
- If Broc says "post now", post now.

## Platform notes

- **TikTok**: the production app is approved, but posts are **inbox drafts** (`mode: inbox`). The
  caption is pushed to Broc's phone via ntfy, and he taps Post. Auto-posting needs TikTok's Direct Post
  audit, and until that passes it can only post private videos. Captions saying "link in the comments"
  mean Broc posts that comment by hand.
- **Instagram/Facebook**: these post through the Snowballer Page's token (Business portfolio "Snowballer LLC").
  The tool can't post FB comments, and IG captions can't hold clickable links ("link in bio").
- **YouTube**: the pinned comment is posted automatically, but Broc pins it by hand. These are also
  manual Studio steps: thumbnails (unless `--thumbnail` is given), end screens, and Shorts covers
  (Shorts can't take an uploaded thumbnail).
- Broc's upload speed is slow (about 0.4 Mbps), so long videos take a while. A "waiting" status (TryLater)
  retries on its own for up to 48 hours. It isn't a failure.

## After it runs, tell Broc

- What posted, with links, and what's scheduled for later.
- Manual steps:
  - Pin the comment on each YouTube channel.
  - Finish the TikTok drafts, plus any "link in the comments" comment.
  - Thumbnail and end screen in Studio.
  - Check the IG bio link.

Keep it short.

## Don't

- Don't edit `.env`, the token files, `config.yaml` or `state.json` by hand, and never print secrets.
- Don't commit or push from `~/social-scheduler`. The code is maintained in the
  `brocamayo/brocamayo` repo and copied here.
- Don't post to accounts outside the routing above.

## Open threads

- Dec 15 prediction (Nvidia video): Broc bet that Claude Pro, ChatGPT Plus or SuperGrok shrinks at the
  same price by Dec 15. Check-ins are due after Nvidia's earnings (Nov 17) and on Dec 15.
- X routing is undecided.
- Giveaway winners were due Nov 9, and a `./sq giveaway` picker was offered.
