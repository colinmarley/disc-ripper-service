"""Tests for AI failure analysis service (Ollama integration)."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from db.database import SessionLocal
from db.models import RipJob


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _make_failed_job(log: str = "Error: makemkvcon exited 1") -> str:
    job_id = str(uuid4())
    with SessionLocal() as db:
        db.add(RipJob(
            id=job_id,
            disc_type="dvd",
            media_type="movie",
            title="Test Movie",
            year=2023,
            status="failed",
            error="makemkvcon exited 1 for title 0",
            mkv_title_indices=[0],
            output_paths=[],
            log=log,
        ))
        db.commit()
    return job_id


def _ollama_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "message": {"role": "assistant", "content": json.dumps(payload)}
    }
    return resp


# ── analyze_failed_job ────────────────────────────────────────────────────────

async def test_analyze_success_saves_analysis():
    from services.analysis_service import analyze_failed_job
    job_id = _make_failed_job()

    mock_resp = _ollama_response({
        "error_type": "disc_read_error",
        "error_summary": "The disc has unreadable sectors.",
        "suggested_fix": "Clean the disc and retry.",
        "claude_prompt": "No code change needed.",
    })

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        analysis = await analyze_failed_job(job_id)

    assert analysis.job_id == job_id
    assert analysis.error_type == "disc_read_error"
    assert analysis.error_summary == "The disc has unreadable sectors."
    assert analysis.suggested_fix == "Clean the disc and retry."


async def test_analyze_success_persists_to_db():
    from services.analysis_service import analyze_failed_job, get_job_analysis
    job_id = _make_failed_job()

    mock_resp = _ollama_response({
        "error_type": "encode_failure",
        "error_summary": "HandBrake failed.",
        "suggested_fix": "Check GPU.",
        "claude_prompt": "No code change needed.",
    })

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        await analyze_failed_job(job_id)

    result = get_job_analysis(job_id)
    assert result is not None
    assert result.error_type == "encode_failure"


async def test_analyze_partial_json_defaults_to_other():
    from services.analysis_service import analyze_failed_job
    job_id = _make_failed_job()

    bad_resp = MagicMock()
    bad_resp.raise_for_status = MagicMock()
    bad_resp.json.return_value = {"message": {"role": "assistant", "content": "not valid json {"}}

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=bad_resp)
        analysis = await analyze_failed_job(job_id)

    assert analysis.error_type == "other"
    assert "could not be parsed" in analysis.error_summary.lower()


async def test_analyze_json_embedded_in_text():
    """Ollama sometimes wraps JSON in prose — the regex fallback should extract it."""
    from services.analysis_service import analyze_failed_job
    job_id = _make_failed_job()

    content = 'Here is the analysis: {"error_type": "configuration_error", "error_summary": "Bad config.", "suggested_fix": "Fix it.", "claude_prompt": "No code change needed."}'
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"message": {"role": "assistant", "content": content}}

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        analysis = await analyze_failed_job(job_id)

    assert analysis.error_type == "configuration_error"


async def test_analyze_non_failed_job_raises():
    from services.analysis_service import analyze_failed_job
    job_id = str(uuid4())
    with SessionLocal() as db:
        db.add(RipJob(
            id=job_id,
            disc_type="dvd",
            media_type="movie",
            title="Test",
            year=2023,
            status="done",
            mkv_title_indices=[0],
            output_paths=[],
        ))
        db.commit()

    with pytest.raises(ValueError, match="not failed"):
        await analyze_failed_job(job_id)


async def test_analyze_missing_job_raises():
    from services.analysis_service import analyze_failed_job
    with pytest.raises(ValueError, match="not found"):
        await analyze_failed_job("totally-fake-id")


async def test_analyze_ollama_error_propagates():
    from services.analysis_service import analyze_failed_job
    job_id = _make_failed_job()

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=Exception("Connection refused")
        )
        with pytest.raises(Exception, match="Connection refused"):
            await analyze_failed_job(job_id)


# ── get_job_analysis ──────────────────────────────────────────────────────────

def test_get_job_analysis_returns_none_when_missing():
    from services.analysis_service import get_job_analysis
    assert get_job_analysis("nonexistent-id") is None


async def test_get_job_analysis_returns_most_recent():
    """Running analyze twice should not break get_job_analysis (returns latest)."""
    from services.analysis_service import analyze_failed_job, get_job_analysis
    job_id = _make_failed_job()

    for error_type in ("disc_read_error", "encode_failure"):
        mock_resp = _ollama_response({
            "error_type": error_type,
            "error_summary": "Summary.",
            "suggested_fix": "Fix.",
            "claude_prompt": "No code change needed.",
        })
        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            await analyze_failed_job(job_id)

    latest = get_job_analysis(job_id)
    assert latest is not None
    assert latest.error_type == "encode_failure"
