"""A scan can ask for titles shorter than MakeMKV's minimum title length, and
a job started from such a scan rips with the same minimum."""

from pathlib import Path
from unittest.mock import patch

import pytest

from services.job_manager import job_manager
from services.makemkv_service import minlength_args, parse_scan, save_scan, scan_min_length
from tests.test_rip_capture import _null_log, _titles, fake_makemkv

FIXTURES = Path(__file__).parent / "fixtures" / "makemkv"


def _scan_via_api(client, params=None):
    """GET /disc/info against a fake makemkvcon; returns (response, argv)."""
    raw = (FIXTURES / "dvd_tv_info.txt").read_text(encoding="utf-8")
    calls: list[tuple] = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return raw.encode(), b""

    async def _exec(*args, **_kwargs):
        calls.append(args)
        return _Proc()

    with patch("services.makemkv_service.makemkvcon_available", return_value=True), \
         patch("services.makemkv_service.asyncio.create_subprocess_exec", new=_exec):
        response = client.get("/disc/info", params=params)
    return response, (calls[0] if calls else None)


def test_default_scan_is_unchanged(client):
    response, argv = _scan_via_api(client)
    body = response.json()
    assert not any(str(a).startswith("--minlength") for a in argv)
    assert argv[-2] == "info"
    assert body["error"] is None and len(body["titles"]) == 3
    assert body["disc"]["skipped_short"] == 1
    # Present (as null) so a caller can tell this build knows the option.
    assert "min_length_seconds" in body["disc"] and body["disc"]["min_length_seconds"] is None
    stored = client.get(f"/disc/scans/{body['scan_id']}").json()
    assert scan_min_length(stored["parsed"]) is None


def test_scan_with_min_length_passes_it_to_makemkv_and_stores_it(client):
    response, argv = _scan_via_api(client, {"min_length": 0})
    body = response.json()
    assert "--minlength=0" in argv
    assert argv.index("--minlength=0") < argv.index("info")
    assert body["disc"]["min_length_seconds"] == 0
    stored = client.get(f"/disc/scans/{body['scan_id']}").json()
    assert scan_min_length(stored["parsed"]) == 0

    response, argv = _scan_via_api(client, {"min_length": 30})
    assert "--minlength=30" in argv
    assert response.json()["disc"]["min_length_seconds"] == 30


def test_min_length_must_be_a_sensible_number(client):
    for bad in ("-1", "99999", "abc"):
        response, argv = _scan_via_api(client, {"min_length": bad})
        assert response.status_code == 422
        assert argv is None


def test_unknown_query_parameters_are_ignored(client):
    # A newer web app may send options this build does not know.
    response, argv = _scan_via_api(client, {"some_future_option": "1"})
    assert response.status_code == 200
    assert response.json()["error"] is None


def test_minlength_helpers():
    assert minlength_args(None) == []
    assert minlength_args(0) == ["--minlength=0"]
    assert minlength_args(45) == ["--minlength=45"]
    assert scan_min_length(None) is None
    assert scan_min_length({}) is None
    assert scan_min_length({"min_length_seconds": None}) is None
    assert scan_min_length({"min_length_seconds": 0}) == 0
    assert scan_min_length({"min_length_seconds": True}) is None


async def _rip_argv(make_job, parsed_extra: dict) -> list[tuple]:
    raw = (FIXTURES / "dvd_tv_info.txt").read_text(encoding="utf-8")
    scan = save_scan(raw, {**parse_scan(raw), **parsed_extra})
    job_id = make_job(
        status="queued", title="Short Titles", mkv_title_indices=[0, 2],
        scan_id=scan["scan_id"], scan_link="declared", fingerprint=scan["fingerprint"],
    )
    calls: list[tuple] = []
    inner = fake_makemkv({0: {"files": ["a_t00.mkv"]}, 2: {"files": ["a_t02.mkv"]}})

    async def _exec(*args, **kwargs):
        calls.append(args)
        return await inner(*args, **kwargs)

    with patch("services.job_manager.asyncio.create_subprocess_exec", new=_exec):
        await job_manager._run_rip(job_id, _null_log)
    assert [r.status for r in _titles(job_id)] == ["ripped", "ripped"]
    return calls


@pytest.mark.asyncio
async def test_rip_uses_the_minimum_length_of_its_scan(make_job):
    calls = await _rip_argv(make_job, {"min_length_seconds": 0})
    assert len(calls) == 2
    for argv in calls:
        assert "--minlength=0" in argv
        assert argv.index("--minlength=0") < argv.index("mkv")
    assert [argv[-2] for argv in calls] == ["0", "2"]


@pytest.mark.asyncio
async def test_rip_from_an_ordinary_scan_is_unchanged(make_job):
    for extra in ({}, {"min_length_seconds": None}):
        calls = await _rip_argv(make_job, extra)
        assert all(not any(str(a).startswith("--minlength") for a in argv) for argv in calls)
