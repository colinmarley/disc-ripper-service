"""
Post-rip checks on a finished MKV: an ffprobe summary and a sha256.

Both run against the file in the local rip directory, before delivery, so
everything in the manifest is known before any file moves.
"""

import hashlib
import json
import os
import subprocess
from typing import Optional

from config.settings import settings

_STREAM_KEYS = (
    "index", "codec_type", "codec_name", "profile", "width", "height",
    "display_aspect_ratio", "avg_frame_rate", "field_order", "pix_fmt",
    "channels", "channel_layout", "sample_rate", "bit_rate",
)
_PROBE_TIMEOUT = 120


def _trim_probe(raw: dict) -> dict:
    fmt = raw.get("format") or {}
    streams = []
    for s in raw.get("streams") or []:
        entry = {k: s[k] for k in _STREAM_KEYS if k in s}
        tags = s.get("tags") or {}
        entry["language"] = tags.get("language")
        entry["title"] = tags.get("title")
        disposition = s.get("disposition") or {}
        entry["default"] = bool(disposition.get("default"))
        entry["forced"] = bool(disposition.get("forced"))
        streams.append(entry)
    chapters = [
        {
            "start": float(c.get("start_time", 0) or 0),
            "end": float(c.get("end_time", 0) or 0),
            "title": (c.get("tags") or {}).get("title"),
        }
        for c in raw.get("chapters") or []
    ]

    def _num(v, cast):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    return {
        "format_name": fmt.get("format_name"),
        "duration_seconds": _num(fmt.get("duration"), float),
        "size_bytes": _num(fmt.get("size"), int),
        "bit_rate": _num(fmt.get("bit_rate"), int),
        "title": (fmt.get("tags") or {}).get("title"),
        "streams": streams,
        "chapter_count": len(chapters),
        "chapters": chapters,
    }


def probe_file(path: str) -> Optional[dict]:
    """ffprobe summary of a file, or None if ffprobe is missing or fails."""
    try:
        result = subprocess.run(
            [
                settings.ffprobe_path, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", "-show_chapters", path,
            ],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return _trim_probe(json.loads(result.stdout))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def sha256_file(path: str, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_file(path: str) -> dict:
    """Size, optional sha256 and ffprobe summary for one ripped file."""
    return {
        "size_bytes": os.path.getsize(path),
        "sha256": sha256_file(path) if settings.hash_ripped_files else None,
        "probe": probe_file(path),
    }
