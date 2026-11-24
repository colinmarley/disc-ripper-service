"""
Letterboxed DVD titles: find bars burned into a 4:3 frame and crop them away.

Some DVDs store a widescreen programme in a 4:3 frame with black bars recorded
into the picture (non-anamorphic). A 16:9 screen then adds side bars of its
own, and the picture sits in a box with black on all four sides. MakeMKV
copies the disc's video as it is, so such a title is re-encoded once, cropped
to the picture (and deinterlaced when the disc is interlaced). Every other
title keeps the disc's video untouched.

The crop is only taken when the bars are the same at every sampled point: a
dark scene can look like bars at one point, never at all of them.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, List, Optional, Sequence

from config.settings import settings

# Points through the title (fractions of its length) that are sampled, and how
# long each sample runs.
SAMPLE_POINTS = (0.1, 0.3, 0.5, 0.7, 0.9)
SAMPLE_SECONDS = 15
# Bars (top and bottom together) must cover at least this much of the frame.
MIN_BAR_FRACTION = 0.10
# Samples may disagree on the bars by this many lines (the detector's rounding).
BAR_TOLERANCE = 2
# Only SD discs are letterboxed this way; HD discs are not 4:3 frames.
MAX_SD_HEIGHT = 576
# The encoded file must run this close to the original.
DURATION_TOLERANCE = 0.01

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


@dataclass(frozen=True)
class Crop:
    width: int
    height: int
    x: int
    y: int

    def filter(self) -> str:
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}"


def most_common_crop(cropdetect_output: str) -> Optional[Crop]:
    """The crop ffmpeg's cropdetect reported most often in one sample."""
    found = _CROP_RE.findall(cropdetect_output or "")
    if not found:
        return None
    (w, h, x, y), _ = Counter(found).most_common(1)[0]
    return Crop(int(w), int(h), int(x), int(y))


def _is_four_by_three(display_aspect_ratio: Optional[str], width: int, height: int) -> bool:
    try:
        if display_aspect_ratio and ":" in display_aspect_ratio:
            num, den = display_aspect_ratio.split(":", 1)
            ratio = Fraction(int(num), int(den))
        else:
            return False
    except (ValueError, ZeroDivisionError):
        return False
    return abs(float(ratio) - 4 / 3) < 0.02


def choose_crop(
    samples: Sequence[Optional[Crop]], width: int, height: int, display_aspect_ratio: Optional[str],
) -> Optional[Crop]:
    """The crop to apply to a title, or None to leave it as it is.

    Only a 4:3 SD frame qualifies, every sample must have been read, all of
    them must put the bars at the same lines, and the bars must be big enough
    to be bars. The crop is the box holding every sample's picture, so a scene
    whose edges the detector found darker never loses picture.
    """
    if not samples or any(s is None for s in samples):
        return None
    if height <= 0 or height > MAX_SD_HEIGHT or not _is_four_by_three(display_aspect_ratio, width, height):
        return None
    crops: List[Crop] = list(samples)  # type: ignore[arg-type]
    tops = [c.y for c in crops]
    bottoms = [c.y + c.height for c in crops]
    if max(tops) - min(tops) > BAR_TOLERANCE or max(bottoms) - min(bottoms) > BAR_TOLERANCE:
        return None
    top, bottom = min(tops), max(bottoms)
    if (top + (height - bottom)) < height * MIN_BAR_FRACTION:
        return None
    left = min(c.x for c in crops)
    right = max(c.x + c.width for c in crops)
    # Even offsets and sizes (4:2:0 video), growing the box rather than cutting picture.
    left -= left % 2
    top -= top % 2
    right = min(width, right + right % 2)
    bottom = min(height, bottom + bottom % 2)
    return Crop(right - left, bottom - top, left, top)


def encode_command(src: str, dst: str, crop: Crop, interlaced: bool) -> List[str]:
    """ffmpeg: the video re-encoded cropped (and deinterlaced); audio, subtitles and chapters copied."""
    filters = []
    if interlaced:
        filters.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
    filters.append(crop.filter())
    return [
        settings.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-i", src,
        "-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
        "-c:v", "libx264", "-preset", settings.letterbox_preset, "-crf", str(settings.letterbox_crf),
        "-tune", "film", "-vf", ",".join(filters), "-pix_fmt", "yuv420p",
        dst,
    ]


def _video_stream(probe: Optional[dict]) -> Optional[dict]:
    for stream in (probe or {}).get("streams") or []:
        if stream.get("codec_type") == "video":
            return stream
    return None


def _sample(path: str, start: float) -> Optional[Crop]:
    try:
        done = subprocess.run(
            [settings.ffmpeg_path, "-hide_banner", "-ss", f"{start:.0f}", "-i", path,
             "-t", str(SAMPLE_SECONDS), "-vf", "cropdetect=24:2:0", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return most_common_crop(done.stderr)


def detect(path: str, probe: Optional[dict]) -> Optional[Crop]:
    """The crop for the ripped file at ``path`` (see ``choose_crop``), or None."""
    video = _video_stream(probe)
    duration = (probe or {}).get("duration_seconds") or 0
    if video is None or duration <= SAMPLE_SECONDS * 2:
        return None
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if not height or height > MAX_SD_HEIGHT or not _is_four_by_three(video.get("display_aspect_ratio"), width, height):
        return None
    samples = [_sample(path, duration * point) for point in SAMPLE_POINTS]
    return choose_crop(samples, width, height, video.get("display_aspect_ratio"))


def is_interlaced(probe: Optional[dict]) -> bool:
    video = _video_stream(probe) or {}
    return (video.get("field_order") or "progressive") not in ("progressive", "unknown", "")


def _duration(path: str) -> Optional[float]:
    try:
        done = subprocess.run(
            [settings.ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        return float(done.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def crop_in_place(path: str, crop: Crop, interlaced: bool, expected_seconds: float) -> Optional[str]:
    """Replace ``path`` with its cropped re-encode. Returns None when done, else why it was kept as it was."""
    tmp = f"{path}.crop.mkv"
    try:
        done = subprocess.run(
            encode_command(path, tmp, crop, interlaced),
            capture_output=True, text=True, timeout=settings.letterbox_timeout_seconds,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _remove(tmp)
        return f"encode did not run: {exc}"
    if done.returncode != 0 or not os.path.isfile(tmp):
        _remove(tmp)
        return f"ffmpeg exited {done.returncode}: {(done.stderr or '').strip()[-300:]}"
    measured = _duration(tmp) or 0
    if expected_seconds and abs(measured - expected_seconds) / expected_seconds > DURATION_TOLERANCE:
        _remove(tmp)
        return f"encoded file runs {measured:.0f}s, the rip {expected_seconds:.0f}s"
    os.replace(tmp, path)
    return None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def describe(crop: Crop, width: int, height: int, interlaced: bool) -> str:
    note = f"Cropped bars burned into the picture: {width}x{height} -> {crop.width}x{crop.height}"
    return note + (", deinterlaced" if interlaced else "")


__all__: Iterable[str] = (
    "Crop", "choose_crop", "crop_in_place", "describe", "detect", "encode_command",
    "is_interlaced", "most_common_crop",
)
