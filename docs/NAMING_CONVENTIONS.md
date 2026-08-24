# Naming Conventions (disc-ripper-service side)

This is this service's half of the naming pipeline — see
my-media-manager's [`docs/architecture/NAMING_CONVENTIONS.md`](https://github.com/colinmarley/my-media-manager/blob/main/docs/architecture/NAMING_CONVENTIONS.md)
(separate repo — this link only resolves once that file has landed on
`main` there) for the full picture spanning both repos, including the
extras-taxonomy category/folder/suffix table (`EXTRA_SUFFIX_TO_FOLDER`
there / `CONTENT_TYPE_SUFFIX` here — keep them in sync if you add a
category). If working locally, both repos are normally checked out as
siblings (`../my-media-manager/docs/architecture/NAMING_CONVENTIONS.md`
relative to this repo's root).

## Delivery folder (`_ingest_folder_name`, `backend/services/job_manager.py`)

```
{title} ({year})                          # no imdb_id
{title} ({year}) [imdbid-{imdb_id}]        # imdb_id supplied
```

Shows additionally get `/Season {season:02d}` appended.

`[imdbid-ttXXXXXXX]` gives my-media-manager's `AutoMatcherService` enough
signal for ≥80% confidence auto-assignment on ingest.

## Delivered filename (`_build_dest_name`)

| Case | Filename |
|---|---|
| Movie, single title | `{title} ({year}).mkv` |
| Movie, multiple titles, no content type | `{title} ({year}) - Version {n}.mkv` |
| Show, episode (from `episode_map` or sequential fallback) | `{title} {episode_code}.mkv` |
| **Title with `content_type` set** | `{title} ({year})-{suffix}.mkv` (or `{title} ({year}) {n}-{suffix}.mkv` for the 2nd+ of the same type) |

`content_type` (from `StartJobRequest.title_content_types`) always wins
over the movie/episode rules above, even on a multi-title disc that would
otherwise get `- Version N` naming.

## Why the suffix, not something richer

Delivery is filesystem-only — `_run_deliver()` moves files into the shared
`/ark/media/jellyfin/ingest` bind mount with no other push to
my-media-manager for naming/classification info (there **is** a push for
disc-catalog linking — see below — but not for content type). The filename
suffix is the only channel available, which is why it has to match
my-media-manager's classifier exactly.

## Physical disc linking (separate from naming)

`catalog_disc_id` on a job does **not** affect naming at all — it's
communicated via a real API call instead, `POST /api/catalog/link-source`
on my-media-manager (`backend/services/catalog_client.py::link_delivered_files`,
called right after delivery). Best-effort: if my-media-manager is
unreachable when this call happens, the rip still completes and the files
still ingest normally — they just won't show up under the disc's linked
files until connected manually.
