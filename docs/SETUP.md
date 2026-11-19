# Setup

## Where this runs

On the machine that physically has the optical drive. The container needs
the drive passed through (`/dev/sr0` plus its `/dev/sgN` SCSI generic node),
a MakeMKV licence on the host, and write access to the ingest folder your
media server / my-media-manager watches.

## MakeMKV license

MakeMKV needs a valid beta key or purchased license, read from
`~/.MakeMKV/settings.conf` on the host and bind-mounted read-only into the
container at `/root/.MakeMKV` (see `docker-compose.yml`; override the host
directory with `MAKEMKV_CONFIG_DIR`).

```bash
# Install MakeMKV on the host (once) — or just create settings.conf by hand
sudo add-apt-repository ppa:heyarje/makemkv-beta
sudo apt-get update && sudo apt-get install -y makemkv-bin makemkv-oss

# Set the license key via the MakeMKV GUI, or edit ~/.MakeMKV/settings.conf:
#   app_Key = "<your key>"
```

## Docker

```bash
cp .env.example .env           # then edit
docker compose up -d           # start / bring up after down
docker compose down            # stop
docker compose logs -f         # tail logs
docker compose up -d --build   # rebuild after code changes
```

Base image is `ubuntu:24.04` (not `python:3.11-slim`) specifically so the
`heyarje/makemkv-beta` PPA can be added and `makemkv-bin`/`makemkv-oss`
installed inside the image. The drive is passed through via Docker's
`devices:` — that plus the license bind-mount are the only two host
dependencies; everything else is self-contained in the image.

## Environment variables

See the configuration table in the [root README](../README.md#configuration)
and [`.env.example`](../.env.example).

## Prerequisites for AI failure analysis

An OpenAI-compatible chat-completions endpoint (LocalAI, Ollama's `/v1`,
vLLM, ...) reachable from inside the container at `LOCALAI_URL`, with
`LOCALAI_ANALYSIS_MODEL` available. This feature is optional — job
create/rip/deliver all work without it; only `POST /jobs/{id}/analyze`
depends on it.
