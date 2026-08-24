"""Tests for encode service branching logic and GPU fallback."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path


def _ffprobe_result(data: dict) -> MagicMock:
    r = MagicMock()
    r.stdout = json.dumps(data)
    return r


# ── _probe_video_width ────────────────────────────────────────────────────────

def test_probe_video_width_returns_width():
    from services.encode_service import _probe_video_width
    with patch("subprocess.run", return_value=_ffprobe_result({"streams": [{"width": 1920}]})):
        assert _probe_video_width("/fake.mkv") == 1920


def test_probe_video_width_no_streams_returns_zero():
    from services.encode_service import _probe_video_width
    with patch("subprocess.run", return_value=_ffprobe_result({"streams": []})):
        assert _probe_video_width("/fake.mkv") == 0


def test_probe_video_width_exception_returns_zero():
    from services.encode_service import _probe_video_width
    with patch("subprocess.run", side_effect=Exception("ffprobe not found")):
        assert _probe_video_width("/fake.mkv") == 0


# ── _has_english_audio ────────────────────────────────────────────────────────

def test_has_english_audio_eng_tag():
    from services.encode_service import _has_english_audio
    data = {"streams": [{"tags": {"language": "eng"}}]}
    with patch("subprocess.run", return_value=_ffprobe_result(data)):
        assert _has_english_audio("/fake.mkv") is True


def test_has_english_audio_en_tag():
    from services.encode_service import _has_english_audio
    data = {"streams": [{"tags": {"language": "en"}}]}
    with patch("subprocess.run", return_value=_ffprobe_result(data)):
        assert _has_english_audio("/fake.mkv") is True


def test_has_english_audio_uppercase_tag():
    from services.encode_service import _has_english_audio
    data = {"streams": [{"tags": {"LANGUAGE": "ENG"}}]}
    with patch("subprocess.run", return_value=_ffprobe_result(data)):
        assert _has_english_audio("/fake.mkv") is True


def test_has_english_audio_foreign():
    from services.encode_service import _has_english_audio
    data = {"streams": [{"tags": {"language": "jpn"}}]}
    with patch("subprocess.run", return_value=_ffprobe_result(data)):
        assert _has_english_audio("/fake.mkv") is False


def test_has_english_audio_no_streams():
    from services.encode_service import _has_english_audio
    with patch("subprocess.run", return_value=_ffprobe_result({"streams": []})):
        assert _has_english_audio("/fake.mkv") is False


# ── process_file: "none" encoder ──────────────────────────────────────────────

async def test_none_encoder_copies_file(tmp_path):
    from services.encode_service import process_file
    src = tmp_path / "input.mkv"
    dst = tmp_path / "output.mkv"
    src.write_bytes(b"fake mkv content")

    logs: list[str] = []

    async def log_cb(line: str):
        logs.append(line)

    await process_file(str(src), str(dst), "dvd", log_callback=log_cb, encoder="none")

    assert dst.exists()
    assert dst.read_bytes() == b"fake mkv content"
    assert any("No-encode" in l for l in logs)


# ── process_file: routing ─────────────────────────────────────────────────────

async def test_bluray_routes_to_remux(tmp_path):
    from services import encode_service
    src = tmp_path / "input.mkv"
    src.write_bytes(b"")

    with patch.object(encode_service, "_probe_video_width", return_value=1920), \
         patch.object(encode_service, "_remux_bluray", new_callable=AsyncMock) as mock_remux:
        await encode_service.process_file(str(src), "/out.mkv", "bluray")

    mock_remux.assert_called_once()


async def test_dvd_routes_to_encode(tmp_path):
    from services import encode_service
    src = tmp_path / "input.mkv"
    src.write_bytes(b"")

    with patch.object(encode_service, "_probe_video_width", return_value=720), \
         patch.object(encode_service, "_encode_dvd", new_callable=AsyncMock) as mock_encode:
        await encode_service.process_file(str(src), "/out.mkv", "dvd")

    mock_encode.assert_called_once()


async def test_dvd_wide_video_auto_becomes_bluray(tmp_path):
    from services import encode_service
    src = tmp_path / "input.mkv"
    src.write_bytes(b"")
    logs: list[str] = []

    async def log_cb(line: str):
        logs.append(line)

    with patch.object(encode_service, "_probe_video_width", return_value=1920), \
         patch.object(encode_service, "_remux_bluray", new_callable=AsyncMock) as mock_remux:
        await encode_service.process_file(str(src), "/out.mkv", "dvd", log_callback=log_cb)

    mock_remux.assert_called_once()
    assert any("overriding disc type to bluray" in l for l in logs)


async def test_dvd_narrow_video_stays_dvd(tmp_path):
    from services import encode_service
    src = tmp_path / "input.mkv"
    src.write_bytes(b"")

    with patch.object(encode_service, "_probe_video_width", return_value=720), \
         patch.object(encode_service, "_encode_dvd", new_callable=AsyncMock) as mock_encode, \
         patch.object(encode_service, "_remux_bluray", new_callable=AsyncMock) as mock_remux:
        await encode_service.process_file(str(src), "/out.mkv", "dvd")

    mock_encode.assert_called_once()
    mock_remux.assert_not_called()


# ── _remux_bluray: ffmpeg command construction ────────────────────────────────

async def test_remux_bluray_english_audio_uses_language_map(tmp_path):
    from services import encode_service
    src = tmp_path / "input.mkv"
    src.write_bytes(b"")

    captured: list[list[str]] = []

    async def fake_run_proc(cmd, log_callback, prefix=""):
        captured.append(cmd)

    with patch.object(encode_service, "_has_english_audio", return_value=True), \
         patch.object(encode_service, "_run_proc", side_effect=fake_run_proc):
        await encode_service._remux_bluray(str(src), "/out.mkv", None)

    assert captured, "expected _run_proc to be called"
    cmd = captured[0]
    assert "0:a:m:language:eng" in cmd
    assert "0:s:m:language:eng?" in cmd


async def test_remux_bluray_foreign_audio_uses_all_tracks(tmp_path):
    from services import encode_service
    src = tmp_path / "input.mkv"
    src.write_bytes(b"")

    captured: list[list[str]] = []

    async def fake_run_proc(cmd, log_callback, prefix=""):
        captured.append(cmd)

    with patch.object(encode_service, "_has_english_audio", return_value=False), \
         patch.object(encode_service, "_run_proc", side_effect=fake_run_proc):
        await encode_service._remux_bluray(str(src), "/out.mkv", None)

    cmd = captured[0]
    # Foreign film: all audio (-map 0:a), no language filter
    assert "0:a" in cmd
    assert "0:a:m:language:eng" not in cmd


# ── _encode_dvd: GPU fallback ─────────────────────────────────────────────────

async def test_encode_dvd_nvenc_failure_falls_back_to_x265(tmp_path):
    from services import encode_service
    src = tmp_path / "input.mkv"
    src.write_bytes(b"")
    dst = tmp_path / "output.mkv"

    call_count = [0]
    logs: list[str] = []

    async def log_cb(line: str):
        logs.append(line)

    async def fake_run_proc(cmd, log_callback, prefix=""):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("nvenc exited 1")
        dst.write_bytes(b"encoded")

    with patch.object(encode_service, "_run_proc", side_effect=fake_run_proc):
        await encode_service._encode_dvd(
            str(src), str(dst), log_cb,
            quality=21, encoder="nvenc_h265",
        )

    assert call_count[0] == 2
    assert any("GPU encoder unavailable" in l or "retrying" in l.lower() for l in logs)


async def test_encode_dvd_non_nvenc_failure_raises(tmp_path):
    from services import encode_service
    src = tmp_path / "input.mkv"
    src.write_bytes(b"")

    async def fake_run_proc(cmd, log_callback, prefix=""):
        raise RuntimeError("x265 exited 1")

    with patch.object(encode_service, "_run_proc", side_effect=fake_run_proc):
        with pytest.raises(RuntimeError, match="x265"):
            await encode_service._encode_dvd(
                str(src), "/out.mkv", None,
                quality=21, encoder="x265",
            )
