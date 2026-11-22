"""
Rip manifest: the complete record of one rip job.

The same payload is written next to the delivered files
(``_rip_manifest.<job8>.json`` in the title's ingest folder), kept on the
ripper's own volume, and pushed to my-media-manager. It carries everything
known at rip time: which catalogued disc, what the disc reported about
itself, every title on it (ripped or not), and for each delivered file the
title it came from and what the user declared it to be.

One manifest per job, so several discs of the same show can deliver into
the same folder without overwriting each other's record.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from config.settings import settings

SCHEMA_VERSION = 1
MANIFEST_PREFIX = "_rip_manifest."

_EPISODE_RE = re.compile(
    r"^\s*S(?P<season>\d{1,3})E(?P<episode>\d{1,4})(?:-?E(?P<end>\d{1,4}))?(?:\s*-\s*(?P<name>.*))?\s*$",
    re.IGNORECASE,
)


# ── Schema ──────────────────────────────────────────────────────────────────

class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class ManifestCatalog(_Model):
    disc_id: Optional[str] = None
    title: Optional[str] = None
    barcode: Optional[str] = None
    disc_number: Optional[int] = None
    edition: Optional[str] = None
    format: Optional[str] = None
    release_id: Optional[str] = None


class ManifestIdentity(_Model):
    media_type: str
    title: str
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    season: Optional[int] = None


class ManifestDisc(_Model):
    disc_type: Optional[str] = None            # as chosen for the job: dvd | bluray
    reported_disc_type: Optional[str] = None   # as MakeMKV reported it
    disc_name: Optional[str] = None
    volume_label: Optional[str] = None
    fingerprint: Optional[str] = None
    fingerprint_algo: Optional[str] = None
    drive_model: Optional[str] = None
    drive_label: Optional[str] = None
    device: Optional[str] = None
    makemkv_version: Optional[str] = None
    scan_id: Optional[str] = None
    scan_link: Optional[str] = None
    scanned_at: Optional[str] = None
    title_count: Optional[int] = None
    skipped_short: Optional[int] = None
    skipped_other: Optional[int] = None


class ManifestTitle(_Model):
    title_index: int
    file_seq: int = 0
    status: str
    error: Optional[str] = None
    content_type: Optional[str] = None
    is_main_feature: Optional[bool] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_end: Optional[int] = None
    tmdb_episode_id: Optional[int] = None
    episode_name: Optional[str] = None
    custom_name: Optional[str] = None
    makemkv_filename: Optional[str] = None
    dest_path: Optional[str] = None
    ingest_relative_path: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    rip_started_at: Optional[str] = None
    rip_finished_at: Optional[str] = None
    read_errors: int = 0
    messages: Optional[list] = None
    probe: Optional[dict] = None


class Manifest(_Model):
    schema_version: int
    job_id: str
    status: str   # delivering | delivered | partial | failed | cancelled
    ripper_host: Optional[str] = None
    created_at: Optional[str] = None
    rip_started_at: Optional[str] = None
    rip_finished_at: Optional[str] = None
    delivered_at: Optional[str] = None
    catalog: ManifestCatalog
    identity: ManifestIdentity
    disc: ManifestDisc
    scan_titles: list[dict] = []
    titles: list[ManifestTitle] = []


# ── Declarations ────────────────────────────────────────────────────────────

def episode_code(decl: dict) -> Optional[str]:
    """Structured declaration → the episode string used in filenames."""
    if decl.get("custom_name"):
        return str(decl["custom_name"])
    if decl.get("episode") is None:
        return None
    code = f"S{int(decl.get('season') or 0):02d}E{int(decl['episode']):02d}"
    if decl.get("episode_end") is not None and decl["episode_end"] != decl["episode"]:
        code += f"-E{int(decl['episode_end']):02d}"
    if decl.get("episode_name"):
        code += f" - {decl['episode_name']}"
    return code


def parse_episode_value(value: str) -> dict:
    """An ``episode_map`` string ('S01E03 - Pilot') → structured fields."""
    m = _EPISODE_RE.match(value or "")
    if not m:
        return {"custom_name": value} if value else {}
    return {
        "season": int(m.group("season")),
        "episode": int(m.group("episode")),
        "episode_end": int(m.group("end")) if m.group("end") else None,
        "episode_name": (m.group("name") or "").strip() or None,
    }


def derive_legacy_maps(titles: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Structured ``titles`` → the (episode_map, title_content_types) pair."""
    episode_map: dict[str, str] = {}
    content_types: dict[str, str] = {}
    for decl in titles:
        key = str(decl["title_index"])
        content_type = decl.get("content_type")
        if content_type and content_type not in ("main_feature", "episode"):
            content_types[key] = content_type
            continue
        code = episode_code(decl)
        if code:
            episode_map[key] = code
    return episode_map, content_types


def declarations_for(job) -> dict[int, dict]:
    """What the user said each selected title is, keyed by disc title index.

    Uses the structured ``title_declarations`` when the job has them and
    fills the gaps from the older ``episode_map`` / ``title_content_types``.
    """
    declared = {int(d["title_index"]): dict(d) for d in (job.title_declarations or [])}
    episode_map = job.episode_map or {}
    content_types = job.title_content_types or {}
    indices = list(job.mkv_title_indices or [])
    main_candidates = [i for i in indices if not content_types.get(str(i))]

    out: dict[int, dict] = {}
    for idx in indices:
        decl = declared.get(idx, {"title_index": idx})
        key = str(idx)
        if not decl.get("content_type") and content_types.get(key):
            decl["content_type"] = content_types[key]
        if decl.get("episode") is None and not decl.get("custom_name") and key in episode_map:
            for k, v in parse_episode_value(episode_map[key]).items():
                if decl.get(k) is None:
                    decl[k] = v
        if not decl.get("content_type"):
            decl["content_type"] = "episode" if job.media_type == "show" else "main_feature"
        if decl.get("is_main_feature") is None:
            decl["is_main_feature"] = (
                job.media_type == "movie"
                and decl["content_type"] == "main_feature"
                and len(main_candidates) == 1
            ) or None
        if job.media_type == "show" and decl.get("season") is None and decl["content_type"] == "episode":
            decl["season"] = job.season
        out[idx] = decl
    return out


# ── Build / write ───────────────────────────────────────────────────────────

def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def ingest_relative(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return Path(path).relative_to(settings.ingest_root).as_posix()
    except ValueError:
        return None


def build_manifest(job, scan, rip_titles: list, catalog_disc: Optional[dict], status: str) -> dict:
    """Assemble the manifest for a job from its DB rows."""
    declarations = declarations_for(job)
    parsed = (scan.parsed if scan is not None else None) or {}
    catalog_disc = catalog_disc or {}

    titles = []
    for rt in rip_titles:
        decl = declarations.get(rt.title_index, {})
        titles.append({
            "title_index": rt.title_index,
            "file_seq": rt.file_seq,
            "status": rt.status,
            "error": rt.error,
            "content_type": decl.get("content_type"),
            "is_main_feature": decl.get("is_main_feature"),
            "season": decl.get("season"),
            "episode": decl.get("episode"),
            "episode_end": decl.get("episode_end"),
            "tmdb_episode_id": decl.get("tmdb_episode_id"),
            "episode_name": decl.get("episode_name"),
            "custom_name": decl.get("custom_name"),
            "makemkv_filename": rt.makemkv_filename,
            "dest_path": rt.dest_path,
            "ingest_relative_path": ingest_relative(rt.dest_path),
            "size_bytes": rt.size_bytes,
            "sha256": rt.sha256,
            "rip_started_at": _iso(rt.rip_started_at),
            "rip_finished_at": _iso(rt.rip_finished_at),
            "read_errors": rt.read_errors or 0,
            "messages": rt.messages,
            "probe": rt.probe,
        })

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job.id,
        "status": status,
        "ripper_host": settings.host_id,
        "created_at": _iso(job.created_at),
        "rip_started_at": _iso(job.rip_started_at),
        "rip_finished_at": _iso(job.rip_finished_at),
        "delivered_at": _iso(job.delivered_at),
        "catalog": {
            "disc_id": job.catalog_disc_id,
            "title": catalog_disc.get("title"),
            "barcode": catalog_disc.get("barcode"),
            "disc_number": _pick(catalog_disc, "discNumber", "disc_number"),
            "edition": catalog_disc.get("edition"),
            "format": catalog_disc.get("format"),
            "release_id": _pick(catalog_disc, "setId", "set_id", "releaseId"),
        },
        "identity": {
            "media_type": job.media_type,
            "title": job.title,
            "year": job.year,
            "imdb_id": job.imdb_id,
            "tmdb_id": job.tmdb_id,
            "season": job.season,
        },
        "disc": {
            "disc_type": job.disc_type,
            "reported_disc_type": scan.disc_type if scan is not None else None,
            "disc_name": scan.disc_name if scan is not None else None,
            "volume_label": scan.volume_label if scan is not None else None,
            "fingerprint": job.fingerprint,
            "fingerprint_algo": scan.fingerprint_algo if scan is not None else None,
            "drive_model": scan.drive_model if scan is not None else None,
            "drive_label": scan.drive_label if scan is not None else None,
            "device": scan.device if scan is not None else None,
            "makemkv_version": scan.makemkv_version if scan is not None else None,
            "scan_id": job.scan_id,
            "scan_link": job.scan_link,
            "scanned_at": _iso(scan.created_at) if scan is not None else None,
            "title_count": scan.title_count if scan is not None else None,
            "skipped_short": parsed.get("skipped_short"),
            "skipped_other": parsed.get("skipped_other"),
        },
        "scan_titles": parsed.get("titles") or [],
        "titles": titles,
    }
    # Validate shape; return the plain dict so unknown keys survive untouched.
    Manifest.model_validate(manifest)
    return manifest


def manifest_filename(job_id: str) -> str:
    return f"{MANIFEST_PREFIX}{job_id[:8]}.json"


def stored_manifest_path(job_id: str) -> str:
    """Durable copy on the ripper's own volume (survives the library moving
    or cleaning the ingest folder)."""
    return str(Path(settings.db_path).parent / "manifests" / f"{job_id}.json")


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def write_manifest(manifest: dict, folder: Optional[str]) -> dict:
    """Write the manifest to the ripper volume and, if given, the delivery
    folder. Returns {"stored": path, "folder": path | None}; a folder write
    failure is reported as None rather than raised."""
    payload = json.dumps(manifest, indent=2, ensure_ascii=False)
    stored = Path(stored_manifest_path(manifest["job_id"]))
    _atomic_write(stored, payload)

    folder_path: Optional[str] = None
    if folder:
        target = Path(folder) / manifest_filename(manifest["job_id"])
        try:
            _atomic_write(target, payload)
            folder_path = str(target)
        except OSError:
            folder_path = None
    return {"stored": str(stored), "folder": folder_path}


def read_stored_manifest(job_id: str) -> Optional[dict]:
    path = Path(stored_manifest_path(job_id))
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
