"""
Wrapper around makemkvcon CLI.

Disc info parsing handles the machine-readable (-r) output format:
  TCOUNT:N
  TINFO:title_idx,attr_id,int_val,str_val
  SINFO:title_idx,stream_idx,attr_id,int_val,str_val

Key TINFO attribute IDs used here:
  2  = name/label
  9  = duration  (H:MM:SS)
  11 = chapter count
  25 = output filename MakeMKV would write
  27 = file size (bytes, as string)

Key SINFO attribute IDs:
  1  = stream type (Video/Audio/Subtitle)
  19 = codec short name
  21 = video width (int_val)
  22 = video height (int_val)
"""

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from config.settings import settings


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


def _parse_info_output(output: str) -> list[dict]:
    """Parse makemkvcon -r info output into a list of title dicts."""
    titles: dict[int, dict] = {}
    streams: dict[int, list[dict]] = {}  # title_idx → list of stream dicts

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("TINFO:"):
            parts = line[6:].split(",", 3)
            if len(parts) < 4:
                continue
            t_idx, attr_id, int_val, str_val = int(parts[0]), int(parts[1]), parts[2], parts[3].strip('"')
            if t_idx not in titles:
                titles[t_idx] = {"index": t_idx, "name": "", "duration_seconds": 0,
                                 "chapter_count": 0, "file_size_bytes": 0,
                                 "output_filename": "", "streams": []}
            if attr_id == 2:
                titles[t_idx]["name"] = str_val
            elif attr_id == 9:
                titles[t_idx]["duration_seconds"] = _parse_duration(str_val)
            elif attr_id == 11:
                titles[t_idx]["chapter_count"] = int(int_val) if int_val.isdigit() else 0
            elif attr_id == 25:
                titles[t_idx]["output_filename"] = str_val
            elif attr_id == 27:
                try:
                    titles[t_idx]["file_size_bytes"] = int(str_val)
                except ValueError:
                    pass

        elif line.startswith("SINFO:"):
            parts = line[6:].split(",", 4)
            if len(parts) < 5:
                continue
            t_idx, s_idx, attr_id, int_val, str_val = (
                int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4].strip('"')
            )
            if t_idx not in streams:
                streams[t_idx] = []
            stream = next((s for s in streams[t_idx] if s["index"] == s_idx), None)
            if stream is None:
                stream = {"index": s_idx, "type": "", "codec": "", "width": 0, "height": 0}
                streams[t_idx].append(stream)
            if attr_id == 1:
                stream["type"] = str_val.lower()
            elif attr_id == 19:
                stream["codec"] = str_val
            elif attr_id == 21:
                try:
                    stream["width"] = int(int_val)
                except ValueError:
                    pass
            elif attr_id == 22:
                try:
                    stream["height"] = int(int_val)
                except ValueError:
                    pass

    for t_idx, t in titles.items():
        t["streams"] = streams.get(t_idx, [])
        video = next((s for s in t["streams"] if s["type"] == "video"), None)
        t["width"] = video["width"] if video else 0
        t["height"] = video["height"] if video else 0
        t["codec"] = video["codec"] if video else ""

    return sorted(titles.values(), key=lambda t: t["index"])


def makemkvcon_available() -> bool:
    return shutil.which(settings.makemkvcon_path) is not None


async def scan_disc() -> dict[str, Any]:
    """Return disc metadata and title list."""
    if not makemkvcon_available():
        return {"error": "makemkvcon not found — install makemkv-bin", "titles": []}

    try:
        proc = await asyncio.create_subprocess_exec(
            settings.makemkvcon_path, "-r", "--noscan", "info", settings.disc_device,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        return {"error": "Disc scan timed out after 120s", "titles": []}
    except FileNotFoundError:
        return {"error": "makemkvcon not found", "titles": []}

    output = stdout.decode(errors="replace")
    if proc.returncode != 0 and not output:
        return {"error": stderr.decode(errors="replace") or "Disc scan failed", "titles": []}

    titles = _parse_info_output(output)
    if not titles:
        return {"error": "No titles found — is a disc inserted?", "titles": []}

    return {"titles": titles, "error": None}


async def rip_title(
    title_index: int,
    output_dir: str,
    log_callback: Optional[callable] = None,
) -> AsyncIterator[str]:
    """
    Rip a single title from the disc into output_dir.
    Yields log lines as they arrive.
    Returns when ripping is complete.
    Raises RuntimeError on non-zero exit.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    cmd = [
        settings.makemkvcon_path,
        "--noscan", "-r",
        "mkv", settings.disc_device,
        str(title_index),
        output_dir,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    lines = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        lines.append(line)
        if log_callback:
            await log_callback(line)

    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"makemkvcon exited {proc.returncode}")
