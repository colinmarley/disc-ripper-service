# disc-ripper-service

A small FastAPI + SQLite service that drives
[MakeMKV](https://www.makemkv.com/)'s `makemkvcon` to scan and rip DVDs and
Blu-rays, names the output the way Jellyfin expects (using
[TMDB](https://www.themoviedb.org/) for title/year/episode lookup), and
delivers the finished files to an ingest folder. When a rip fails, it can ask
an LLM behind any OpenAI-compatible endpoint to read the log and suggest a
fix.

It is the ripping half of
[my-media-manager](https://github.com/colinmarley/my-media-manager), but it
runs fine on its own with its built-in web UI.

## Features

- Disc scan: title list with duration, resolution, codec and chapter count
- Queued rip jobs (one at a time) with live log streaming over SSE
- Movie / TV naming, including `[imdbid-tt…]` folder tags and extras
  suffixes (`-trailer`, `-featurette`, …) — see
  [docs/NAMING_CONVENTIONS.md](docs/NAMING_CONVENTIONS.md)
- Delivery to an ingest folder (e.g. one Jellyfin or my-media-manager watches)
- Retry / cancel, in-browser preview and rename of delivered files
- Optional LLM failure analysis (`POST /jobs/{id}/analyze`)
- Optional linking of rips to my-media-manager's physical-disc catalog
- Plain-JS UI served at `/`

## Requirements

- **Linux host with an optical drive.** The container needs the drive passed
  through: the block device (`/dev/sr0`) and its SCSI generic node
  (`/dev/sgN` — find it with `lsscsi -g`). Blu-ray needs a drive MakeMKV
  supports.
- **Docker** with Compose v2.24+ (for the optional `env_file` entry).
- **A MakeMKV licence key.** MakeMKV is free while in beta; the current beta
  key is posted on the [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?t=1053),
  or buy a licence. The key lives in the host's `~/.MakeMKV/settings.conf`
  (`app_Key = "..."`), which is mounted read-only into the container. MakeMKV
  itself is installed inside the image from the `heyarje/makemkv-beta` PPA.
- **A TMDB API key** (free, v3) for title search. Without it the `/tmdb/*`
  routes return `503`; ripping still works.
- *Optional:* an OpenAI-compatible chat endpoint (LocalAI, Ollama `/v1`,
  vLLM, …) for failure analysis.
- *Optional:* a running my-media-manager backend for disc-catalog linking.

## Quick start

```bash
git clone https://github.com/colinmarley/disc-ripper-service.git
cd disc-ripper-service
cp .env.example .env
# edit .env: TMDB_API_KEY, DISC_SG_DEVICE_PATH, INGEST_DIR, ...
mkdir -p ~/.MakeMKV && echo 'app_Key = "<your key>"' >> ~/.MakeMKV/settings.conf
docker compose up -d --build
curl http://localhost:8083/health   # {"status":"ok"}
```

Open `http://<host>:8083/` for the UI. More detail in
[docs/SETUP.md](docs/SETUP.md).

## Configuration

### Service settings

Read by [`backend/config/settings.py`](backend/config/settings.py) (pydantic
settings; environment variables or a `.env` in the working directory).
Defaults are container paths.

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8083` | Listen port inside the container |
| `RIPS_ROOT` | `/data/media/rips` | Intermediate rip output |
| `INGEST_ROOT` | `/data/ingest` | Final delivery folder. File paths under it are sent to my-media-manager, so mount it at the path my-media-manager sees |
| `DB_PATH` | `/data/ripper/ripper.db` | SQLite job database |
| `LOGS_ROOT` | `/data/ripper/logs` | Full per-job log files (the DB keeps only a 500-line tail) |
| `MAKEMKVCON_PATH` | `makemkvcon` | MakeMKV CLI |
| `HANDBRAKE_PATH` | `HandBrakeCLI` | Reserved for encoding (not used by the current pipeline) |
| `FFMPEG_PATH` | `ffmpeg` | Reserved (not used by the current pipeline) |
| `FFPROBE_PATH` | `ffprobe` | Reserved (not used by the current pipeline) |
| `DISC_DEVICE` | `disc:0` | MakeMKV disc identifier |
| `SCAN_TIMEOUT_SECONDS` | `300` | How long to wait for a disc scan |
| `HASH_RIPPED_FILES` | `true` | Record a sha256 of every ripped file in the manifest (about a minute per 30 GB) |
| `VERIFY_DISC_MATCHES_SCAN` | `true` | Fail a job if the disc in the drive is not the one its scan was taken from |
| `REQUIRE_CATALOG_DISC` | `true` | A rip must name a disc in the my-media-manager catalog (`catalog_disc_id`) |
| `LIBRARY_SYNC_INTERVAL_SECONDS` | `60` | How often unsent rip records are retried against my-media-manager |
| `LOCALAI_URL` | `http://localhost:8080` | OpenAI-compatible base URL (`/v1/chat/completions` is appended). Must be reachable from inside the container |
| `LOCALAI_API_KEY` | *(empty)* | Bearer token for that endpoint |
| `LOCALAI_ANALYSIS_MODEL` | `qwen2.5-14b` | Model name for failure analysis |
| `MEDIA_MANAGER_API_URL` | `http://localhost:8082` | my-media-manager backend, for disc-catalog search/create/link |
| `TMDB_API_KEY` | *(empty)* | TMDB v3 key; unset → `503` on `/tmdb/*` |
| `TMDB_BASE_URL` | `https://api.themoviedb.org/3` | TMDB API base |

Logging (`backend/homelab_logging/config.py`): `HOST_ID`, `LOG_PROJECT`,
`LOG_SERVICE`, `LOG_LEVEL` (default `INFO`), `LOG_FORMAT` (`json` default, or
`console`). Logs are structured JSON on stdout.

### docker compose only

| Variable | Default | Purpose |
|---|---|---|
| `RIPPER_PORT` | `8083` | Published host port |
| `DISC_DEVICE_PATH` | `/dev/sr0` | Host optical block device |
| `DISC_SG_DEVICE_PATH` | `/dev/sg0` | Host SCSI generic node for the drive |
| `RIPS_DIR` | `./data/rips` | Host directory for intermediate rips |
| `INGEST_DIR` | `./data/ingest` | Host delivery directory, mounted at `INGEST_ROOT` |
| `MAKEMKV_CONFIG_DIR` | `$HOME/.MakeMKV` | Host directory with `settings.conf` |

## API overview

No authentication — run it on a trusted network only. Full reference in
[docs/API.md](docs/API.md).

| Area | Endpoints |
|---|---|
| Disc | `GET /disc/info` (scan), `GET /disc/status` |
| Jobs | `POST /jobs/start`, `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/retry`, `POST /jobs/{id}/stop`, `GET /jobs/{id}/log` (SSE) |
| Files | `GET /jobs/{id}/files/{n}/stream`, `PATCH /jobs/{id}/files/{n}` (rename) |
| Analysis | `POST /jobs/{id}/analyze`, `GET /jobs/{id}/analysis` |
| TMDB proxy | `GET /tmdb/search`, `GET /tmdb/{media_type}/{id}`, `GET /tmdb/show/{id}/season/{n}` |
| Misc | `GET /health`, `GET /` (UI) |

## Using it with my-media-manager

my-media-manager's disc-ripper admin page calls this API to scan discs and
start jobs. The two meet in two places:

1. **The ingest folder.** Point `INGEST_DIR`/`INGEST_ROOT` at the folder
   my-media-manager watches. Folder and file names carry everything it needs
   to auto-match (title, year, `[imdbid-…]`, extras suffix) — see
   [docs/NAMING_CONVENTIONS.md](docs/NAMING_CONVENTIONS.md).
2. **The disc catalog.** Set `MEDIA_MANAGER_API_URL`. A job started with a
   `catalog_disc_id` calls my-media-manager's
   `POST /api/catalog/link-source` after delivery. This is best-effort: if
   my-media-manager is down, the rip still completes.

## Development and tests

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest                # unit + API tests; no drive or network needed
python main.py        # run locally on :8083 (needs makemkvcon on PATH to scan)
```

There is also a small Playwright smoke suite for the UI that runs against a
live instance:

```bash
npm install && npx playwright install chromium
BASE_URL=http://localhost:8083 npm run test:e2e
```

## License

[MIT](LICENSE) © 2026 Colin Marley
