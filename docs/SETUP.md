# Setup

## Where this runs

**Must run on ai-workstation** — the optical drive (`/dev/sr0`), the `/ark`
NAS mount, and the MakeMKV license all live there. There is no viable way
to run this on another homelab machine.

## MakeMKV license

MakeMKV needs a valid beta key or purchased license, read from
`~/.MakeMKV/settings.conf` on the host and bind-mounted read-only into the
container at `/root/.MakeMKV` (see `docker-compose.yml`).

```bash
# Install MakeMKV on the host (once)
sudo add-apt-repository ppa:heyarje/makemkv-beta
sudo apt-get update && sudo apt-get install -y makemkv-bin makemkv-oss

# Set the license key via the MakeMKV GUI, or edit ~/.MakeMKV/settings.conf directly
```

## Docker

```bash
cd /data/code/disc-ripper-service
docker compose up -d           # start / bring up after down
docker compose down            # stop
docker compose logs -f         # tail logs
docker compose up -d --build   # rebuild after code changes
```

Base image is `ubuntu:24.04` (not `python:3.11-slim`) specifically so the
`heyarje/makemkv-beta` PPA can be added and `makemkv-bin`/`makemkv-oss`
installed inside the image. `/dev/sr0` is passed through via Docker's
`devices:` — that plus the license bind-mount are the only two host
dependencies; everything else is self-contained in the image.

## Environment variables

| Variable | Default | Note |
|---|---|---|
| `RIPS_ROOT` | `/data/media/rips` | Intermediate rip output — local NVMe, avoids NAS I/O during the rip itself |
| `INGEST_ROOT` | `/ark/media/jellyfin/ingest` | Final delivery — watched by my-media-manager |
| `DB_PATH` | `/data/ripper/ripper.db` | SQLite job database |
| `LOGS_ROOT` | `/data/ripper/logs` | Full per-job log files (uncapped — the DB `log` column only keeps a 500-line tail) |
| `MAKEMKVCON_PATH` | `makemkvcon` | Path to the MakeMKV CLI |
| `DISC_DEVICE` | `disc:0` | MakeMKV disc device identifier |
| `OLLAMA_HOST` | `http://192.168.0.227:11434` | Ollama API base for AI failure analysis — must be the host LAN IP, not `localhost`, since this service runs in Docker |
| `OLLAMA_ANALYSIS_MODEL` | `qwen2.5:32b` | Model used for failure log analysis |
| `MEDIA_MANAGER_API_URL` | `http://192.168.0.227:8082` | my-media-manager's backend — disc catalog search/create/link |
| `TMDB_API_KEY` | *(unset)* | Required for `/tmdb/*` search endpoints; unset means `503` on those routes |
| `TMDB_BASE_URL` | `https://api.themoviedb.org/3` | |

## Prerequisites for AI failure analysis

Ollama running on the ai-workstation host (not in Docker) with the
`qwen2.5:32b` model already pulled. This feature is optional — job
create/rip/deliver all work without it; only `POST /jobs/{id}/analyze`
depends on Ollama being reachable.
