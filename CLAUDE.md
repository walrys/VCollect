# VCollect

## Overview

A web app that converts a growing database of saved video URLs on Notion into a managed video collection with a web gallery / interface.


## Goals

### PROCESSING
- Using Notion API, grab data source which contains video urls, captions, tags, created date (date url was saved)
- Only processing rows that have not been marked as complete,
For every row:
1) Download video with all metadata from URL using yt-dlp (instaloader for Instagram carousels that mix videos and images)
2) Compress video using ffmpeg for local storage
3) Extract thumbnails from video if the downloader did not grab it
4) Summarise video transcript (if present) into a AI summarised description
5) Using Notion API, mark item in data table as complete when all is done

### Gallery Web Page:
- VCOLLECT (header)
- At the top of the page is the main quick search bar
- Beneath it are tags you can click on to add to the search, sorted by most commonly used tags
- Display all saved videos in a grid of thumbnails in a timeline order (latest to oldest)
- Adding tags instantly filters the thumbnails visible in the grid
- Typing in to search can take a second or two

Menu options (top right)
- "Sync" button. This button will trigger the PROCESSING steps listed above. If currently processing, a progress bar should show in place of the button.
- Group-by-Date Checkbox: When enabled, adds sub-headings to group the thumbnails. Group by -> Today, Yesterday, This Week, Last Week, By Month

Detailed decisions for both live under Architecture.

## Non-goals

v1 deliberately leaves out:
- Login and multiple users: one user, on a private network (LAN or Tailscale).
- Paid services of any kind: every AI step runs locally (Ollama, whisper).
- Public share links.
- Search by meaning: search is keyword full-text.
- Processing without pressing Sync: the nightly run exists but ships off.
- A native app. An iOS-only app is a future consideration; the JSON API is built for it.

## Tech stack

- Python 3.12 + FastAPI, SQLite with FTS5, a static HTML page + plain JS (no build step) calling the app's own JSON API.
- yt-dlp: every link first, Instagram reels included. Self-updates on container start and nightly, since its site extractors break often. Needs Deno (bundled in the image) to solve YouTube's JavaScript challenges.
- instaloader: Instagram carousels that mix videos and images, which yt-dlp cannot fetch whole. Works without a login, through an Instagram API rate-limited separately from, and far more tightly than, the pages yt-dlp reads. Self-updates with yt-dlp.
- ffmpeg: compression, keyframes, thumbnails.
- Ollama + `qwen3.5` (vision-capable, multilingual): summaries and AI tags.
- whisper via speaches (OpenAI-compatible server), model `Systran/faster-whisper-small.en`: transcripts.
- Docker Compose. Runs on the dev PC first (Ryzen 7 7700, 31 GB RAM, RTX 5070 Ti 16 GB, Docker Desktop on WSL2); may move to ZimaOS (Intel N100, ~8 GB free RAM, CPU-only AI).

## Architecture

### Services (`docker-compose.yml`)
- `app`: FastAPI web server and the background worker in one container, with ffmpeg, yt-dlp, instaloader.
- `ollama`: `qwen3.5:9b` on the PC GPU, `qwen3.5:4b` or `2b` on the N100 (`OLLAMA_MODEL`).
- `whisper`: local speech-to-text, on the CPU on every machine (see Open questions).
- `docker-compose.gpu.yml` gives `ollama` the NVIDIA GPU (dev PC only). `docker-compose.dev.yml` mounts the source and reloads on save. Each machine's `.env` picks its files through `COMPOSE_FILE`, so every command is plain `docker compose …`.
- `OLLAMA_URL` / `WHISPER_URL` are config, so an `app` on ZimaOS can send AI work to the PC when it is on.
- `DATA_DIR` (default `./data`, gitignored) holds the SQLite DB and all media. Moving machines means copying this folder.
- Every setting lives in `.env`; `.env.example` lists them with defaults.

### Notion data source
- The user's database is "Saved Video Links" (163 rows on 2026-10-24, mostly Instagram reels). Existing properties: `Link` (url), `Caption` (the title column, holding the user's own note), `Tags` (multi-select), `Created time`, and an unused `💾` checkbox the app ignores. The page's built-in created time is the **saved date**.
- On startup the app adds any missing: `Status` (Not started / Done / Failed), `Error` (text), `Summary` (text), `AI Tags` (multi-select). If the API refuses to create the Status type, stop with a message asking the user to add it by hand.
- Property names are configurable in `.env` (`NOTION_PROP_*`). The defaults are generic (`URL`, …), so this database needs `NOTION_PROP_URL=Link`.
- Transcripts and media stay local; Notion never receives them.

### Sync with Notion (two-way)
- **Push**: every gallery edit (Notion tags including brand-new options, caption, summary, AI tags, delete, Add URL) goes to Notion as soon as it is saved. Edits are written locally first and listed in `pending_edits` until Notion has them; if Notion is unreachable they wait, and the next pull (launch, page load or Sync) sends them before reading Notion back. Delete needs Notion: when it cannot be reached, nothing is deleted.
- **Pull**: a full read of every row (100 per request) on app launch, on every page load or refresh, and at the start of every Sync. A load within 60 s of the last pull reuses it (Notion allows ~3 requests/s). Pulls run in the background: the page renders from SQLite and updates in place. Each pull also refreshes the "Sync (N new)" count.
- **Auto-pull**: a 5-minute pull is a menu toggle, off by default. The server runs it (stored in `app_settings`), so it keeps going with no tab open; open tabs check back every 30 s and redraw only when something changed. Notion webhooks need a public HTTPS endpoint, so polling is the only option.
- **Conflicts**: per field, the newest edit wins.
- **Delete in the gallery**: after a confirm, the post and its files are deleted at once and the Notion page moves to Notion's trash (the API can only trash; Notion empties trash after 30 days).
- **Deleted in Notion**: a row missing from a pull flags its post **deleted-in-Notion**; it stays visible with a badge. The next Sync opens a dialog before processing (thumbnails, all ticked): delete the ticked posts, keep the rest. Kept posts become **local-only**: badge "Not in Notion", still editable, never prompted again, and "Re-add to Notion" in the pop-up creates a fresh row (the post keeps its first saved date, although Notion dates the row today). Restoring the page from Notion's trash clears the flag on the next pull. Deleting a post that is no longer in Notion never calls Notion.

### Sync job (processing)
- Runs only from the Sync button, plus an optional nightly run (`NIGHTLY_SYNC`, off by default). New rows wait for the button.
- A row's Retry on the Processing page (Failed and To retry rows) processes that one row at once: next in a running Sync (moving up if the Sync has it queued; the row in hand is left alone), else in a run of its own that skips the pull and leaves every other row for the next Sync. Retry all only sets the Failed rows back to Not started for the next Sync.
- One job at a time, one row at a time (platform rate limits). Server-side, so it survives closing the tab. Every open tab shows the progress bar ("12 / 40 · compressing <title>") with a cancel. Posts join the grid as each finishes.
- Rows to process: `Status` = Not started. "Sync (N new)" counts only rows no Sync has tried: a row a Sync leaves for later keeps the reason (`retry_error`) and is retried by every Sync, but is no longer new.
- Per row:
  1. **Normalise the URL**: strip tracking params (`?igsh=`, `&si=`), rewrite `youtu.be` / `shorts/` to `watch?v=`. A duplicate of an existing post → Failed "Duplicate of <link to first row>". A playlist or channel → Failed "not a single post".
  2. **Download**: yt-dlp first for every link. On Instagram it takes the single-file MP4 (H.264 + AAC ≤720p, which Compress copies as is) over the separate VP9 streams, and waits 3–8 s per download. When yt-dlp finds an Instagram image post or carousel (`NotASingleVideo`), the page it read already lists every picture at full size, so image-only posts are saved from those URLs without asking Instagram again; a carousel with videos goes to instaloader. Both read `DATA_DIR/cookies.txt`. Keep every item of a carousel, images included. Image-only posts are saved as images. A login wall or rate limit (`LoginNeeded`) skips the rest of that site's posts for the run, and the skipped posts keep its reason. instaloader's is named "Instagram image posts" so it never stops reels or image-only posts; since a post's kind is only known once yt-dlp has read it, the downloader checks that skip itself.
  3. **Compress**: videos to H.264 + AAC MP4, ≤720p, CRF 26, 96 kbps audio, `+faststart`; skip the re-encode when the source is already H.264 ≤720p and small. Originals are discarded. Images to WebP q80, longest side ≤1440 px, keeping whichever file is smaller. Thumbnails are WebP, extracted from the video when none was downloaded.
  4. **Transcript**: YouTube captions when present, else whisper. YouTube rate-limits caption downloads (HTTP 429) on their own, so a caption failure retries the download without them. A missing transcript is never a failure. speaches answers HTTP 500 for audio over 30 s with no speech in it (music only), so a 500 counts as no speech; an unreachable whisper is still retried next Sync.
  5. **Summarise**: one Ollama call per post returns JSON `{summary, ai_tags}`: a 2–3 sentence English summary and 3–5 AI tags (lowercase, singular). Input: transcript, user caption, platform caption, every image of an image post, and `KEYFRAMES` (default 4, 0 disables) frames per video. The prompt carries the existing AI-tag vocabulary so tags get reused.
  6. **Finish**: write `Summary`, `AI Tags`, and `Status` = Done to Notion.
- Failure rules:
  - Every step is idempotent: a finished step's output is reused, never redone.
  - Done only when every step succeeds.
  - Transient errors (network, rate limit, Ollama or whisper down) leave the row Not started for the next Sync.
  - Permanent errors (removed, private, unsupported) → Failed with the reason in `Error`. Failed rows stay skipped until retried on the Processing page (which sets `Status` back to Not started in Notion, and with a row's Retry processes it at once) or reset in Notion.
  - A post appears in the gallery once its media and thumbnail are ready; its summary can follow later.
- `MAX_DURATION_MIN` (off by default): longer videos → Failed "too long".
- Disk cap 100 GB: the menu shows usage; Sync refuses new downloads above 95 GB and deletes nothing on its own.

### JSON API
The web page talks to the server only through the JSON API (`/api/posts`, `/api/tags`, `/api/sync`, …), so a future iOS app reuses it unchanged. Media is served with HTTP range requests (seeking, iOS AVPlayer).

### Gallery details
- **Header**: stays on screen as a translucent bar holding the wordmark, the search bar, a **+** button whose panel adds a URL by creating a Notion row (warns on duplicates), and the menu. `/` focuses search.
- **Search**: server-side, debounced, keyword FTS5 with prefix matching over title, user caption, platform caption, summary, transcript, uploader, AI tags. Results keep timeline order and combine with selected tags as AND.
- **Tag bar**: Notion tags as filled chips; AI tags in a second row as outlined chips. Top ~20 plus "show all"; on a phone each row is one line that scrolls sideways. Filtering is instant (client-side) and AND across tags. Once tags are selected, the bar shows only tags present in the current results, with counts, and a "12 of 162 posts · Clear" line appears.
- **Grid**: one 9:16 tile per post (landscape centre-cropped), ordered by saved date, static thumbnails, a "1/5" badge on carousels and an icon on image posts.
- **Group by date**: buckets are exclusive, first match wins: Today, Yesterday, This Week (weeks start Monday), Last Week, then one heading per month ("October 2026"). Empty groups are hidden. Grouping runs in the browser, in its local time. The checkbox state is remembered per browser.
- **Pop-up player**: plays local media. It grows out of the tapped tile and shrinks back into the tile of the post it closes on. Swipe (the media follows the finger) or arrow keys move through a post's items and through the filtered posts; pull down, Escape or Back closes it. Motion uses springs and falls back to cross-fades under reduced motion. Edits Notion tags, caption, summary, AI tags; promotes an AI tag to a Notion tag; deletes; links to the original.
- **Processing page** (`#processing`, linked from the ☰ panel and the last-Sync report): every row Sync has not finished, newest saved first, in three groups: Failed (Notion's `Error`, with Retry and Retry all), To retry (why the last Sync left it, with Retry) and New. It shares the header, so Sync and the progress bar stay in reach, and refreshes as a Sync works through rows.
- **Menu**: the Sync button (or progress bar), then a ☰ panel with the Processing link, group-by-date, the auto-pull toggle and disk usage against `DISK_CAP_GB`.
- Responsive for phone browsers over Tailscale; follows the system dark/light theme.

## Directory layout

```
app/                     FastAPI package
  main.py                app factory; mounts the API and the static web page
  config.py              Settings, read from environment variables
  api/                   JSON API routes: health, pull, sync, posts + search, processing (unfinished
                         rows, retry), settings (auto-pull, disk usage); /media serves DATA_DIR/media
  db/                    SQLite connection, append-only MIGRATIONS, tag and search helpers
  web/                   static page, ES modules, no build: app.js (Sync, Add link, services), gallery.js
                         (grid, tag bar, search, date groups), player.js, editor.js (edit mode),
                         menu.js (☰ panel), deleted.js (deleted-in-Notion dialog),
                         processing.js (#processing page), tags.js, toast.js, spring.js (springs and
                         gesture helpers for the player's motion), app.css
  notion/                client, schema check, pull; mirror.py runs every Notion conversation
                         (pull, push, create, trash) under one lock
  pipeline/              Sync: urls, download, media (ffmpeg), transcribe, summarise; post.py runs
                         one post's steps, runner.py the background job; errors.py decides
                         Failed (PostFailed) vs retry next Sync (TryLater)
tests/                   pytest; fakes/ holds FakeNotion, fake downloader/whisper/Ollama, and
                         sample media that ffmpeg generates per test session
docker/entrypoint.sh     refreshes yt-dlp and instaloader on every server start
docker-compose*.yml      base stack, dev override, GPU override
.env.example             every setting, plus the per-machine COMPOSE_FILE
data/                    DATA_DIR default (gitignored)
```

## Commands

```
install: cp .env.example .env              # then fill in; set COMPOSE_FILE for this machine
         docker compose build
         docker compose up -d ollama && docker compose exec ollama ollama pull qwen3.5:9b   # once per machine
run:     docker compose up                 # gallery at http://localhost:8000
test:    docker compose run --rm --no-deps app pytest
lint:    docker compose run --rm --no-deps app sh -c "ruff check . && ruff format --check ."
build:   docker compose build
```

## Conventions

- Develop inside Docker: source mounted, uvicorn reloads on save. The Windows host needs only Docker Desktop.
- Each external system (Notion, yt-dlp, instaloader, ffmpeg, Ollama, whisper) sits behind a small interface so tests swap in a fake.
- Tests run offline: fakes for Notion and the downloaders, plus the clip in `tests/fixtures/` for real ffmpeg and whisper checks.
- Pipeline steps check for their own output first, so a rerun resumes.
- Lint and format with ruff.
- In development, saving any `.py` file under `app/` restarts the server, which stops a running Sync (the next press resumes it). Page files (`app/web/`) reload without a restart.

## Domain notes

- **Post**: one Notion row ↔ one post, holding one or more **items** (video or image). Tiles, summaries, and AI tags are per post.
- **Saved date**: the Notion row's created time. It drives ordering and date groups; the platform upload date is metadata only.
- **User caption** vs **platform caption**: `Caption` in Notion is the user's note; the platform caption comes from the downloader. Stored separately, both searchable.
- **Notion tags** vs **AI tags**: separate types in separate Notion properties and separate tag-bar rows. Promoting copies an AI tag into Notion tags.
- **Pull** = Notion → app. **Push** = gallery edit → Notion. **Sync** = the button: pull, the deleted-in-Notion prompt, then process new rows.
- **Deleted-in-Notion** and **local-only**: post states defined under Sync with Notion.
- Logins: YouTube and Instagram reels, image posts and carousels download without one. Reddit and age-restricted Instagram posts need a session in `DATA_DIR/cookies.txt`, exported from secondary accounts, since Instagram flags scraping accounts.
- The pipeline writes Notion first and local copies second (`posts.generated` holds a summary until Notion has it), so a pull mid-Sync can never undo a finished step.

## Open questions

- Whisper on the GPU: speaches' pinned CUDA images are built on CUDA 12.4 / 12.6 (latest release 0.9.0-rc.3, Dec 2025), and the RTX 5070 Ti (Blackwell) needs 12.8+. Whisper therefore runs on the CPU, which the Ryzen 7700 handles easily for `small.en`. Revisit if speaches ships a 12.8+ build.
- Instagram's anonymous limit for instaloader is low: a burst of ~25 queries got "401 Please wait a few minutes" on 2026-10-25, and it still held an hour later while yt-dlp kept working. Only carousels with videos depend on it now. instaloader signing in from `cookies.txt` should raise the limit; untested until `cookies.txt` exists.
- N100 throughput is unmeasured: time a Sync on ZimaOS before deciding to move there.
- Future: an iOS-only native app on the JSON API.
