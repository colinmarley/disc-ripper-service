"""
Post-rip processing:
  - DVD  → HandBrakeCLI with NVENC H.265 (GPU re-encode, reduces size)
  - Blu-ray → ffmpeg stream copy, English audio/subtitle tracks only (lossless remux)
"""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Optional

from config.settings import settings


def _probe_video_width(file_path: str) -> int:
    """Return video width via ffprobe, 0 on failure."""
    try:
        result = subprocess.run(
            [
                settings.ffprobe_path, "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-select_streams", "v:0",
                file_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            return int(streams[0].get("width", 0))
    except Exception:
        pass
    return 0


def _has_english_audio(file_path: str) -> bool:
    """Return True if file contains at least one English audio stream."""
    try:
        result = subprocess.run(
            [
                settings.ffprobe_path, "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-select_streams", "a",
                file_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        for s in data.get("streams", []):
            tags = s.get("tags", {})
            lang = tags.get("language", "") or tags.get("LANGUAGE", "")
            if lang.lower() in ("eng", "en"):
                return True
    except Exception:
        pass
    return False


async def process_file(
    input_path: str,
    output_path: str,
    disc_type: str,
    log_callback: Optional[callable] = None,
) -> None:
    """
    Encode or remux input_path → output_path.
    disc_type: "dvd" | "bluray"
    Raises RuntimeError on failure.
    """
    # Auto-detect: if video is wider than threshold, treat as Blu-ray regardless of declared type
    width = _probe_video_width(input_path)
    effective_type = disc_type
    if width > settings.bluray_width_threshold and disc_type == "dvd":
        effective_type = "bluray"
        if log_callback:
            await log_callback(f"[encode] Video width {width}px detected — overriding disc type to bluray")

    if effective_type == "bluray":
        await _remux_bluray(input_path, output_path, log_callback)
    else:
        await _encode_dvd(input_path, output_path, log_callback)


async def _remux_bluray(
    input_path: str,
    output_path: str,
    log_callback: Optional[callable],
) -> None:
    """ffmpeg stream-copy: keep video + English audio/subtitles only."""
    has_eng = _has_english_audio(input_path)
    if log_callback:
        await log_callback(f"[remux] Input: {input_path}")
        await log_callback(f"[remux] English audio found: {has_eng}")

    if has_eng:
        # Select video + English audio + English subs (? = optional, no error if absent)
        map_args = [
            "-map", "0:v",
            "-map", "0:a:m:language:eng",
            "-map", "0:s:m:language:eng?",
        ]
    else:
        # Foreign film — keep all audio, copy all subs
        map_args = ["-map", "0:v", "-map", "0:a", "-map", "0:s?"]

    cmd = [
        settings.ffmpeg_path, "-y",
        "-i", input_path,
        *map_args,
        "-c", "copy",
        output_path,
    ]
    await _run_proc(cmd, log_callback, prefix="[remux]")


async def _encode_dvd(
    input_path: str,
    output_path: str,
    log_callback: Optional[callable],
) -> None:
    """HandBrakeCLI: NVENC H.265, audio passthrough, all subtitles."""
    if log_callback:
        await log_callback(f"[encode] Input: {input_path}")
        await log_callback(f"[encode] Encoder: {settings.dvd_encoder}, quality: {settings.dvd_quality}")

    cmd = [
        settings.handbrake_path,
        "-i", input_path,
        "-o", output_path,
        "--encoder", settings.dvd_encoder,
        "--quality", str(settings.dvd_quality),
        "--all-audio",
        "--aencoder", "copy",
        "--all-subtitles",
    ]
    await _run_proc(cmd, log_callback, prefix="[encode]")


async def _run_proc(
    cmd: list[str],
    log_callback: Optional[callable],
    prefix: str = "",
) -> None:
    """Run a subprocess, stream stdout+stderr to log_callback, raise on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if log_callback:
            await log_callback(f"{prefix} {line}" if prefix else line)
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}")
