"""
Wrapper around makemkvcon CLI.

Disc info parsing handles the machine-readable (-r) output format:
  MSG:code,flags,count,"message","format",params...
  DRV:index,visible,enabled,flags,"drive name","disc name","device"
  TCOUNT:N
  CINFO:attr_id,code,"value"
  TINFO:title_idx,attr_id,code,"value"
  SINFO:title_idx,stream_idx,attr_id,code,"value"

Every attribute is kept (as a raw ``attrs`` id → value map) on the disc,
each title and each stream, so nothing MakeMKV reports is lost even where
this module does not give it a name. Attribute ids follow MakeMKV's
``apdefs.h`` (``ap_iaXxx``); the ones named here:

  1  type                 14 audio channel count   26 segment map
  2  name                 15 angle info            27 output filename
  3  language code        16 source file name      28 metadata language code
  4  language name        17 audio sample rate     29 metadata language name
  5  codec id             19 video size (WxH)      30 tree info (description)
  6  codec short          20 video aspect ratio    32 volume name
  7  codec long           21 video frame rate      38 mkv flags
  8  chapter count        22 stream flags          39 mkv flags text
  9  duration             24 original title id     40 audio channel layout
  11 size in bytes        25 segment count
  13 bitrate
"""

import asyncio
import hashlib
import json
import shutil
from typing import Any, Optional
from uuid import uuid4

from config.settings import settings

FINGERPRINT_ALGO = "scan-v1"
# Titles shorter than this are left out of the fingerprint so it does not
# depend on MakeMKV's own minimum-title-length setting.
FINGERPRINT_MIN_SECONDS = 120

# AP_AVStreamFlag bit for forced subtitles (apdefs.h)
_STREAM_FLAG_FORCED = 4096


def _parse_duration(s: str) -> int:
    """'H:MM:SS' → total seconds."""
    parts = s.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    return 0


def _to_int(s: Any, default: int = 0) -> int:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return default


def split_robot_fields(payload: str) -> list[str]:
    """Split the part of a robot-mode line after ``PREFIX:`` into fields.

    Fields are comma separated; string fields are double-quoted and may
    contain commas and backslash-escaped quotes.
    """
    fields: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    n = len(payload)
    while i < n:
        ch = payload[i]
        if in_quotes:
            if ch == "\\" and i + 1 < n:
                buf.append(payload[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quotes = False
            else:
                buf.append(ch)
        elif ch == '"':
            in_quotes = True
        elif ch == ",":
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    fields.append("".join(buf))
    return fields


def parse_drv_line(line: str) -> Optional[dict]:
    """Parse a ``DRV:`` line; None for the blank placeholder entries."""
    if not line.startswith("DRV:"):
        return None
    f = split_robot_fields(line[4:])
    if len(f) < 7:
        return None
    model, label, device = f[4].strip(), f[5].strip(), f[6].strip()
    if not model and not label and not device:
        return None
    return {
        "index": _to_int(f[0], -1),
        "visible": _to_int(f[1]),
        "enabled": _to_int(f[2]),
        "flags": _to_int(f[3]),
        "model": model,
        "label": label,
        "device": device,
    }


def _new_title(idx: int) -> dict:
    return {
        "index": idx, "name": "", "title_name": "", "tree_info": "",
        "duration_seconds": 0, "chapter_count": 0, "file_size_bytes": 0,
        "output_filename": "", "source_file": "", "original_title_id": "",
        "segment_count": 0, "segment_map": "", "angle": "",
        "lang_code": "", "lang_name": "",
        "streams": [], "attrs": {},
    }


def _new_stream(idx: int) -> dict:
    return {
        "index": idx, "type": "", "codec": "", "codec_id": "", "codec_long": "",
        "lang_code": "", "lang_name": "", "name": "",
        "channels": 0, "channel_layout": "", "sample_rate": 0, "bitrate": "",
        "width": 0, "height": 0, "aspect": "", "frame_rate": "",
        "flags": 0, "forced": False, "default": False, "mkv_flags": "",
        "attrs": {},
    }


def _apply_title_attr(t: dict, attr_id: int, val: str) -> None:
    t["attrs"][str(attr_id)] = val
    if attr_id == 2:
        t["title_name"] = val
    elif attr_id == 8:
        t["chapter_count"] = _to_int(val)
    elif attr_id == 9:
        t["duration_seconds"] = _parse_duration(val)
    elif attr_id == 11:
        t["file_size_bytes"] = _to_int(val)
    elif attr_id == 15:
        t["angle"] = val
    elif attr_id == 16:
        t["source_file"] = val
    elif attr_id == 24:
        t["original_title_id"] = val
    elif attr_id == 25:
        t["segment_count"] = _to_int(val)
    elif attr_id == 26:
        t["segment_map"] = val
    elif attr_id == 27:
        t["output_filename"] = val
    elif attr_id == 28:
        t["lang_code"] = val
    elif attr_id == 29:
        t["lang_name"] = val
    elif attr_id == 30:
        t["tree_info"] = val


def _apply_stream_attr(s: dict, attr_id: int, val: str) -> None:
    s["attrs"][str(attr_id)] = val
    if attr_id == 1:
        s["type"] = val.lower()
    elif attr_id == 2:
        s["name"] = val
    elif attr_id == 3:
        s["lang_code"] = val
    elif attr_id == 4:
        s["lang_name"] = val
    elif attr_id == 5:
        s["codec_id"] = val
    elif attr_id == 6:
        s["codec"] = val
    elif attr_id == 7:
        s["codec_long"] = val
    elif attr_id == 13:
        s["bitrate"] = val
    elif attr_id == 14:
        s["channels"] = _to_int(val)
    elif attr_id == 17:
        s["sample_rate"] = _to_int(val)
    elif attr_id == 19:
        if "x" in val:
            w, h = val.split("x", 1)
            s["width"], s["height"] = _to_int(w), _to_int(h)
    elif attr_id == 20:
        s["aspect"] = val
    elif attr_id == 21:
        s["frame_rate"] = val
    elif attr_id == 22:
        s["flags"] = _to_int(val)
        s["forced"] = bool(s["flags"] & _STREAM_FLAG_FORCED)
    elif attr_id == 38:
        s["mkv_flags"] = val
        s["default"] = "d" in val.lower()
    elif attr_id == 40:
        s["channel_layout"] = val


def parse_scan(output: str) -> dict:
    """Parse ``makemkvcon -r info`` output into disc, drive, titles and messages."""
    titles: dict[int, dict] = {}
    streams: dict[int, dict[int, dict]] = {}
    disc_attrs: dict[str, str] = {}
    drives: list[dict] = []
    messages: list[dict] = []
    version = ""
    skipped_short = 0
    skipped_other = 0

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("TINFO:"):
            f = split_robot_fields(line[6:])
            if len(f) < 4:
                continue
            t_idx, attr_id = _to_int(f[0], -1), _to_int(f[1], -1)
            if t_idx < 0 or attr_id < 0:
                continue
            _apply_title_attr(titles.setdefault(t_idx, _new_title(t_idx)), attr_id, f[3])

        elif line.startswith("SINFO:"):
            f = split_robot_fields(line[6:])
            if len(f) < 5:
                continue
            t_idx, s_idx, attr_id = _to_int(f[0], -1), _to_int(f[1], -1), _to_int(f[2], -1)
            if t_idx < 0 or s_idx < 0 or attr_id < 0:
                continue
            title_streams = streams.setdefault(t_idx, {})
            _apply_stream_attr(title_streams.setdefault(s_idx, _new_stream(s_idx)), attr_id, f[4])

        elif line.startswith("CINFO:"):
            f = split_robot_fields(line[6:])
            if len(f) >= 3:
                disc_attrs[str(_to_int(f[0], -1))] = f[2]

        elif line.startswith("DRV:"):
            drv = parse_drv_line(line)
            if drv:
                drives.append(drv)

        elif line.startswith("MSG:"):
            f = split_robot_fields(line[4:])
            if len(f) < 4:
                continue
            code, text = _to_int(f[0], -1), f[3]
            messages.append({"code": code, "text": text})
            if code == 1005 and len(f) > 5:
                version = f[5]
            elif code == 1005:
                version = text.replace(" started", "")
            if code == 3025:
                skipped_short += 1
            elif "skipped" in text.lower():
                skipped_other += 1

    for t_idx, t in titles.items():
        t["streams"] = [streams[t_idx][k] for k in sorted(streams.get(t_idx, {}))]
        # "name" stays the description line the UI has always shown; the real
        # title name (attr 2) is often empty on DVDs.
        t["name"] = t["tree_info"] or t["title_name"]
        video = next((s for s in t["streams"] if s["type"] == "video"), None)
        t["width"] = video["width"] if video else 0
        t["height"] = video["height"] if video else 0
        t["codec"] = video["codec"] if video else ""

    # The drive holding a disc is the one reporting a disc label; fall back
    # to the first real drive entry.
    drive = next((d for d in drives if d["label"]), None) or (drives[0] if drives else None)

    disc = {
        "disc_type": disc_attrs.get("1", ""),
        "name": disc_attrs.get("2", ""),
        "volume_label": disc_attrs.get("32", ""),
        "lang_code": disc_attrs.get("28", ""),
        "lang_name": disc_attrs.get("29", ""),
        "tree_info": disc_attrs.get("30", ""),
        "attrs": disc_attrs,
    }

    return {
        "makemkv_version": version,
        "drive": drive,
        "drives": drives,
        "disc": disc,
        "titles": sorted(titles.values(), key=lambda t: t["index"]),
        "messages": messages,
        "skipped_short": skipped_short,
        "skipped_other": skipped_other,
    }


def _parse_info_output(output: str) -> list[dict]:
    """Parse makemkvcon -r info output into a list of title dicts."""
    return parse_scan(output)["titles"]


def fingerprint(parsed: dict) -> Optional[str]:
    """Stable identity for a disc, computed from its scan structure alone.

    sha256 over the disc type, volume name and the sorted set of title
    shapes (duration, size, chapters, segment map, source file). Sorting
    makes it independent of title order; the minimum-length floor makes it
    independent of MakeMKV's short-title setting. Returns None when the scan
    has no titles.
    """
    titles = parsed.get("titles") or []
    if not titles:
        return None
    long_titles = [t for t in titles if t.get("duration_seconds", 0) >= FINGERPRINT_MIN_SECONDS]
    shapes = sorted(
        [
            t.get("duration_seconds", 0),
            t.get("file_size_bytes", 0),
            t.get("chapter_count", 0),
            t.get("segment_map", ""),
            t.get("source_file", ""),
        ]
        for t in (long_titles or titles)
    )
    disc = parsed.get("disc") or {}
    canonical = json.dumps(
        {
            "algo": FINGERPRINT_ALGO,
            "disc_type": disc.get("disc_type", ""),
            "volume": disc.get("volume_label") or disc.get("name", ""),
            "titles": shapes,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def makemkvcon_available() -> bool:
    return shutil.which(settings.makemkvcon_path) is not None


def save_scan(raw_output: str, parsed: dict) -> dict:
    """Persist a scan (raw text + parsed structure) and return its summary."""
    from db.database import SessionLocal
    from db.models import DiscScan

    disc = parsed.get("disc") or {}
    drive = parsed.get("drive") or {}
    fp = fingerprint(parsed)
    scan = DiscScan(
        id=str(uuid4()),
        raw_output=raw_output,
        parsed=parsed,
        fingerprint=fp,
        fingerprint_algo=FINGERPRINT_ALGO if fp else None,
        disc_type=disc.get("disc_type") or None,
        disc_name=disc.get("name") or None,
        volume_label=disc.get("volume_label") or None,
        drive_model=drive.get("model") or None,
        drive_label=drive.get("label") or None,
        device=drive.get("device") or None,
        makemkv_version=parsed.get("makemkv_version") or None,
        title_count=len(parsed.get("titles") or []),
    )
    with SessionLocal() as db:
        db.add(scan)
        db.commit()
        db.refresh(scan)
        return scan_summary(scan)


def scan_summary(scan) -> dict:
    return {
        "scan_id": scan.id,
        "scanned_at": scan.created_at.isoformat() if scan.created_at else None,
        "fingerprint": scan.fingerprint,
        "fingerprint_algo": scan.fingerprint_algo,
        "disc_type": scan.disc_type,
        "disc_name": scan.disc_name,
        "volume_label": scan.volume_label,
        "drive_model": scan.drive_model,
        "drive_label": scan.drive_label,
        "device": scan.device,
        "makemkv_version": scan.makemkv_version,
        "title_count": scan.title_count,
    }


async def scan_disc() -> dict[str, Any]:
    """Scan the disc, persist the full result, and return disc + title info."""
    if not makemkvcon_available():
        return {"error": "makemkvcon not found — install makemkv-bin", "titles": []}

    timeout = settings.scan_timeout_seconds
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.makemkvcon_path, "-r", "--noscan", "info", settings.disc_device,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"error": f"Disc scan timed out after {timeout}s", "titles": []}
    except FileNotFoundError:
        return {"error": "makemkvcon not found", "titles": []}

    output = stdout.decode(errors="replace")
    err_output = stderr.decode(errors="replace")

    if proc.returncode != 0 and not output:
        return {"error": err_output.strip() or "Disc scan failed", "titles": []}

    parsed = parse_scan(output)
    titles = parsed["titles"]
    if not titles:
        # Surface the raw makemkvcon output to help diagnose the cause
        # (expired beta key, no disc, device error, etc.)
        detail_lines = [
            line for line in (output + err_output).splitlines()
            if line.strip() and not line.startswith(("DRV:", "PRGT:", "PRGC:", "PRGV:"))
        ]
        detail = " | ".join(detail_lines[-5:]) if detail_lines else ""
        msg = "No titles found — is a disc inserted?"
        if detail:
            msg += f"  (makemkvcon: {detail})"
        return {"error": msg, "titles": []}

    summary = save_scan(output, parsed)
    return {
        "titles": titles,
        "error": None,
        "scan_id": summary["scan_id"],
        "disc": {**summary, "skipped_short": parsed["skipped_short"], "skipped_other": parsed["skipped_other"]},
    }
