# VCollect

Turns a Notion database of saved video links into a local, searchable video gallery. Press **Sync** and each new link is downloaded, compressed, transcribed and summarised by local AI. The result is written back to Notion, and the post shows up in a web gallery on your own network.

Everything runs in Docker on your own machine. Nothing is paid or cloud-hosted except Notion itself.

The design, its decisions and how it behaves are documented in [CLAUDE.md](CLAUDE.md). This file covers only setup.

## What you need

- **Docker** with Compose v2.24 or newer (`docker compose version`). On Windows that means Docker Desktop with the WSL2 backend. The host needs nothing else: Python, ffmpeg, yt-dlp and the rest live inside the images.
- **A Notion database** of saved links, with a URL property and access to create an integration for it.
- **Disk space**: roughly 10 GB for the images and AI models, plus your media (the app caps itself at `DISK_CAP_GB`, 100 GB by default).
- **Optional: an NVIDIA GPU** for Ollama. You need a recent driver and, on Windows, Docker Desktop's WSL2 GPU support. Without a GPU everything runs on the CPU, only slower.

## 1. Get the code

```sh
git clone <repo-url> vcollect
cd vcollect
cp .env.example .env
```

Every setting lives in `.env`, and `.env.example` explains each one. `.env` is gitignored, so it stays out of commits.

## 2. Connect Notion

1. Open <https://www.notion.so/profile/integrations> and click **New integration**, type **Internal**. Give it the **Read content**, **Update content** and **Insert content** capabilities. Copy the secret into `NOTION_TOKEN`.
2. Open your database in Notion, then **⋯ → Connections** and add the integration.
3. Copy the 32-character ID from the database's URL (the part before `?v=`) into `NOTION_DATA_SOURCE_ID`. A data source ID also works.
4. Match the property names to your database. The defaults are `URL`, `Caption` and `Tags`. If your link column is called `Link`, for example, set:

   ```sh
   NOTION_PROP_URL=Link
   ```

   `Caption` may be the title column or a text property.

On first start the app adds the properties it writes, if they are missing: `Status` (Not started / Done / Failed), `Error`, `Summary` and `AI Tags`. If Notion refuses to create the `Status` property, `/api/health` says so. In that case add it by hand as a **Status** property with the options `Not started`, `Done` and `Failed`, then restart the app.

Rows with an empty `Status` count as Not started, so your existing rows are all picked up by the first Sync.

## 3. Pick the compose files for this machine

`COMPOSE_FILE` in `.env` decides which compose files load, so every command below stays a plain `docker compose …`.

| Machine | `COMPOSE_FILE` | `OLLAMA_MODEL` |
|---|---|---|
| Dev PC with an NVIDIA GPU (live reload) | `docker-compose.yml:docker-compose.dev.yml:docker-compose.gpu.yml` | `qwen3.5:9b` |
| Dev PC without a GPU | `docker-compose.yml:docker-compose.dev.yml` | `qwen3.5:4b` |
| Always-on server, CPU only (e.g. ZimaOS on an N100) | `docker-compose.yml` | `qwen3.5:4b` or `qwen3.5:2b` |

- `docker-compose.dev.yml` mounts the source and reloads the server whenever you save a file.
- `docker-compose.gpu.yml` gives Ollama the NVIDIA GPU.
- Whisper runs on the CPU on every machine (see *Open questions* in CLAUDE.md).

Also worth setting:

- `PORT`: the gallery's port on the host (default `8000`).
- `DATA_DIR`: where the database and all media live (default `./data`).
- `TZ`: your IANA timezone, e.g. `Europe/London`.

On a slow CPU, `KEYFRAMES=0` skips sending video frames to the model, which makes summaries much faster.

## 4. Build and download the models

```sh
docker compose build
docker compose up -d ollama
docker compose exec ollama ollama pull qwen3.5:9b    # use the model in your OLLAMA_MODEL
```

You only pull the Ollama model once per machine; it is kept in the `ollama-models` volume. The whisper model (`WHISPER_MODEL`) downloads by itself the first time the `whisper` container starts, and is kept in `whisper-models`.

## 5. Start it

```sh
docker compose up -d
```

Open <http://localhost:8000>, or `http://<machine>:<PORT>` from another device on your LAN or Tailscale. The gallery is empty until the first Sync.

To check that everything is connected:

```sh
curl http://localhost:8000/api/health
```

Every service should report ready: `notion.configured: true` with `error: null`, `ollama.reachable` and `ollama.model_ready` both `true`, and `whisper.reachable: true`. On the very first start, give whisper a few minutes to download its model.

Press **Sync** in the gallery to process every new row. Progress shows in place of the button. Rows that fail, or that a Sync leaves for later, are listed on the Processing page (☰ → Processing).

## 6. Optional: logins for Instagram image posts and Reddit

YouTube and Instagram reels download without a login. Instagram image posts and carousels, and Reddit, need a logged-in browser session:

1. Sign in to the site in a browser **with a secondary account**. Instagram flags accounts that scrape, so keep your main one out of it.
2. Export the cookies in Netscape `cookies.txt` format with a browser extension.
3. Save the file as `cookies.txt` inside `DATA_DIR` (by default `./data/cookies.txt`).

The next Sync picks it up; no restart needed. Until then, those posts are left for later with a "needs a login" reason on the Processing page.

## Running AI on another machine

`OLLAMA_URL` and `WHISPER_URL` can point anywhere. To run the app on a low-power server but send AI work to the GPU PC, set this in the server's `.env`:

```sh
OLLAMA_URL=http://<pc>:11434
```

The PC's Ollama port must be published for this: add `ports: ["11434:11434"]` to its `ollama` service. While the PC is off, Sync leaves the affected rows for the next run instead of failing them.

## Day-to-day

```sh
docker compose up -d                 # start
docker compose logs -f app           # follow the server log
docker compose restart app           # restart; also refreshes yt-dlp and gallery-dl
docker compose down                  # stop (data and models are kept)
```

The `app` container upgrades yt-dlp and gallery-dl every time it starts, because their site extractors break often. If downloads start failing, restart `app` first.

**Updating:**

```sh
git pull
docker compose build
docker compose up -d
```

With the dev override the source is mounted, so you only need to rebuild when `Dockerfile` or `requirements*.txt` change.

**Moving to another machine:** stop the stack and copy the whole `DATA_DIR` folder. It holds the SQLite database, all media and `cookies.txt`. Then follow steps 1–5 on the new machine and point `DATA_DIR` at the copy.

## Development

Development happens inside Docker. With the dev override, saving a `.py` file under `app/` restarts the server, which stops a running Sync (pressing Sync again resumes it). Changes to `app/web/` only need a browser reload.

```sh
docker compose run --rm --no-deps app pytest
docker compose run --rm --no-deps app sh -c "ruff check . && ruff format --check ."
```

Tests run offline. They use fakes for Notion, the downloaders, whisper and Ollama, and use the real ffmpeg inside the image.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `/api/health` shows a Notion `error` | Check the integration is connected to the database (step 2), check `NOTION_DATA_SOURCE_ID`, and check the `NOTION_PROP_*` names. The message names any property with the wrong type. |
| `ollama.model_ready: false` | The model in `OLLAMA_MODEL` hasn't been pulled. Run the `ollama pull` from step 4. |
| Ollama ignores the GPU | Check that `COMPOSE_FILE` includes `docker-compose.gpu.yml`, and that `docker run --rm --gpus all ubuntu nvidia-smi` works on the host. |
| Rows stay under "To retry" | Their reason is on the Processing page. Network errors, rate limits and AI services being down are retried by every Sync. |
| "needs a login" | Add or refresh `DATA_DIR/cookies.txt` (step 6). Session cookies expire, so export them again. |
| Sync refuses to download | Disk usage is above 95% of `DISK_CAP_GB` (shown in the ☰ menu). Delete posts or raise the cap. |
