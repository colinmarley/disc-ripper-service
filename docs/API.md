# API Reference

Base URL: `http://<host>:8083` (ai-workstation, port 8083). No authentication —
this service is only reachable on the homelab LAN, same trust model as its
caller (my-media-manager's disc-ripper admin page).

## Disc (`/disc`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/disc/info` | Scan the disc drive via `makemkvcon`, return title list (duration, resolution, codec, chapter count per title) |
| GET | `/disc/status` | `{"makemkvcon_available": bool}` |

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
| GET | `/jobs/{id}/log` | Server-Sent Events stream of live job log output |
| POST | `/jobs/{id}/analyze` | Trigger Ollama AI failure analysis (failed jobs only, ~30-90s) |
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
  "catalog_disc_id": "my-media-manager Disc.id (optional)",
  "title_content_types": {"2": "trailer"}
}
```

`title_content_types` maps a title index to an extras-taxonomy category
slug (see [NAMING_CONVENTIONS.md](./NAMING_CONVENTIONS.md)) — that title
gets routed to a suffix-based filename instead of normal movie/episode
naming, taking priority over `episode_map` for the same index.

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
