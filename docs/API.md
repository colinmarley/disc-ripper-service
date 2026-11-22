# API Reference

Base URL: `http://<host>:8083` (default port 8083). No authentication —
run it only on a trusted LAN, same trust model as its
caller (my-media-manager's disc-ripper admin page).

## Disc (`/disc`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/disc/info` | Scan the disc drive via `makemkvcon`, **store the full scan**, and return `scan_id`, a `disc` block (label, volume name, reported disc type, drive, MakeMKV version, fingerprint) and the title list (duration, chapters, source playlist, segment map, every stream with language/channels/flags) |
| GET | `/disc/status` | `{"makemkvcon_available": bool}` |
| GET | `/disc/scans` | Recent stored scans, newest first. `?fingerprint=` finds earlier scans of the same disc |
| GET | `/disc/scans/{scan_id}` | A stored scan: summary plus everything parsed from it |
| GET | `/disc/scans/{scan_id}/raw` | The untouched `makemkvcon -r info` output (text) |

The disc **fingerprint** (`scan-v1`) is a sha256 over the reported disc
type, volume name and the sorted shapes (duration, size, chapter count,
segment map, source file) of every title of 120 s or more. It needs no
mount of the disc and is the same on every scan of the same pressing.

## Jobs (`/jobs`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/jobs/start` | Create and enqueue a rip job. `409` if a job with the same title+year+disc_type is already active. See [Request body](#start-job-request-body) below. |
| POST | `/jobs/{id}/retry` | Clone a failed/cancelled job's config into a new queued job |
| POST | `/jobs/{id}/stop` | Cancel a queued/running job |
| GET | `/jobs` | List jobs, optional `?status=` filter |
| GET | `/jobs/{id}` | Job detail |
| GET | `/jobs/{id}/files/{file_index}/stream` | Stream a delivered output file (range-request support, for in-browser preview) |
| PATCH | `/jobs/{id}/files/{file_index}` | Rename a delivered file on disk (`status` must be `done`) |
| GET | `/jobs/{id}/titles` | Per-file records: source title index, MakeMKV's own filename, delivered path, size, sha256, read errors, rip times |
| GET | `/jobs/{id}/manifest` | The rip manifest (see below), `404` until one is written |
| POST | `/jobs/{id}/sync` | Send this job's rip record to my-media-manager now |
| POST | `/jobs/sync-all` | Send every record my-media-manager has not taken yet; `?all=true` re-sends all delivered jobs (backfill) |
| GET | `/jobs/{id}/log` | Server-Sent Events stream of live job log output |
| POST | `/jobs/{id}/analyze` | Trigger LLM failure analysis (OpenAI-compatible endpoint) (failed jobs only, ~30-90s) |
| GET | `/jobs/{id}/analysis` | Fetch a saved analysis, `404` if none exists yet |

### Start Job request body

```json
{
  "disc_type": "dvd | bluray",
  "media_type": "movie | show",
  "title": "string",
  "year": 2010,
  "imdb_id": "tt1234567 (optional)",
  "season": 1,
  "mkv_title_indices": [0, 1, 2],
  "episode_map": {"0": "S01E01", "1": "S01E02"},
  "catalog_disc_id": "my-media-manager Disc.id (required unless REQUIRE_CATALOG_DISC=false)",
  "title_content_types": {"2": "trailer"},
  "tmdb_id": 1437,
  "scan_id": "from GET /disc/info (optional)",
  "titles": [
    {"title_index": 0, "season": 1, "episode": 1, "episode_end": 2,
     "tmdb_episode_id": 85395, "episode_name": "Serenity"},
    {"title_index": 2, "content_type": "trailer"}
  ]
}
```

`titles` is the structured form of what each selected title is
(`content_type` is `main_feature`, `episode` or an extras category;
`custom_name` replaces the episode code with free text). When it is sent,
`episode_map` and `title_content_types` are derived from it unless they are
also sent explicitly.

`scan_id` ties the job to one stored scan. If it is omitted the job is
attached to the most recent scan from the last six hours and marked
`scan_link: "inferred"`. During the rip the disc in the drive is checked against the scan (the
drive's disc label where it reports one, and the titles MakeMKV finds on
the disc): a mismatch fails a job with a declared scan, and detaches an
inferred one with a warning in the log. `VERIFY_DISC_MATCHES_SCAN=false`
turns the check off.

`title_content_types` maps a title index to an extras-taxonomy category
slug (see [NAMING_CONVENTIONS.md](./NAMING_CONVENTIONS.md)) — that title
gets routed to a suffix-based filename instead of normal movie/episode
naming, taking priority over `episode_map` for the same index.

## Catalog disc requirement

With `REQUIRE_CATALOG_DISC=true` (the default) `POST /jobs/start` answers:

- `400` when `catalog_disc_id` is missing, or is not a disc in the my-media-manager catalog;
- `503` when the catalog could not be asked.

Set `REQUIRE_CATALOG_DISC=false` to rip without a catalogued disc.

## Sending the record to my-media-manager

The manifest is also pushed to `PUT {MEDIA_MANAGER_API_URL}/api/rips/{job_id}`:
once before any file is moved (status `delivering`, so the library expects
the files) and once after delivery (`delivered` / `partial`).

A push that fails never fails the rip. The outcome is on the job as
`library_sync_state` (`synced`, `failed`, `pending`) and
`library_sync_error`; unsent records are retried every
`LIBRARY_SYNC_INTERVAL_SECONDS` (60) and by `POST /jobs/sync-all`. If the
push after delivery fails, the older `POST /api/catalog/link-source` call is
made as a fallback so a library without the rip endpoint still gets the
disc link. Renaming a delivered file marks the record `pending` again.

## Rip manifest

Every job writes one JSON manifest (`schema_version: 1`):

- next to the delivered files, as `_rip_manifest.<first 8 chars of job id>.json`
  in the title's ingest folder (written **before** any file is moved, then
  rewritten once delivery finishes);
- on the service's own volume, under `manifests/` beside the database
  (also written for failed and cancelled jobs) — this is what
  `GET /jobs/{id}/manifest` returns.

| Block | Contents |
|---|---|
| top level | `job_id`, `status` (`delivering`, `delivered`, `partial`, `failed`, `cancelled`), `ripper_host`, created / rip start / rip finish / delivered times |
| `catalog` | `disc_id` plus a snapshot of the catalogued disc (title, barcode, disc number, edition, format, release id) |
| `identity` | `media_type`, `title`, `year`, `imdb_id`, `tmdb_id`, `season` |
| `disc` | chosen and reported disc type, disc name, volume label, fingerprint, drive, MakeMKV version, scan id |
| `scan_titles[]` | every title on the disc, ripped or not, with all streams and raw attributes |
| `titles[]` | one entry per output file: `title_index`, declared content type / season / episode / TMDB episode id, MakeMKV's filename, `dest_path`, `ingest_relative_path`, size, sha256, read errors, ffprobe summary |

## TMDB (`/tmdb`)

Proxies TMDB so the API key (`TMDB_API_KEY` env var) stays server-side.

| Method | Path | Purpose |
|---|---|---|
| GET | `/tmdb/search?query=&media_type=` | Search movies (`media_type=movie`) or shows (`media_type=show`) |
| GET | `/tmdb/{media_type}/{tmdb_id}` | Full details for a single movie/show |
| GET | `/tmdb/show/{series_id}/season/{season_number}` | Season details including episode list |

Returns `503` if `TMDB_API_KEY` isn't configured, `502` if the upstream TMDB request fails.

## Health

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{"status": "ok"}` |
| GET | `/` | The plain-JS static UI (`backend/static/index.html`) |
