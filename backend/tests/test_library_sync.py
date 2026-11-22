"""Pushing rip records to my-media-manager, the retry outbox, and the rule
that a rip names its catalogued disc."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from config.settings import settings
from db.database import SessionLocal
from db.models import RipJob
from services import catalog_client
from services.job_manager import job_manager
from tests.test_rip_capture import CATALOG_DISC, _null_log, fake_makemkv

MOVIE = {"disc_type": "bluray", "media_type": "movie", "title": "Gone Girl", "year": 2014}


def _job(job_id: str) -> RipJob:
    with SessionLocal() as db:
        return db.get(RipJob, job_id)


def _http_client(response=None, error=None):
    """A patched httpx.AsyncClient whose get/put return `response` or raise `error`."""
    client = MagicMock()
    call = AsyncMock(side_effect=error) if error else AsyncMock(return_value=response)
    client.get = client.put = call
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("services.catalog_client.httpx.AsyncClient", return_value=ctx), call


def _resp(status: int, text: str = "") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = text
    return resp


async def _run(job_id: str, put, link=None):
    link = link or AsyncMock(return_value=True)
    with patch("services.job_manager.asyncio.create_subprocess_exec",
               new=fake_makemkv({0: {"files": ["t00.mkv"]}})), \
         patch("services.job_manager.catalog_client.get_disc", new=AsyncMock(return_value=CATALOG_DISC)), \
         patch("services.job_manager.catalog_client.put_rip_record", new=put), \
         patch("services.job_manager.catalog_client.link_delivered_files", new=link):
        await job_manager._run_rip(job_id, _null_log)
        await job_manager._run_deliver(job_id, _null_log)
    return link


# ── Client ──────────────────────────────────────────────────────────────────

async def test_put_rip_record_success():
    patcher, call = _http_client(_resp(200))
    with patcher:
        assert await catalog_client.put_rip_record("job-1", {"job_id": "job-1"}) == (True, None)
    assert call.call_args.args[0] == f"{settings.media_manager_api_url}/api/rips/job-1"
    assert call.call_args.kwargs["json"] == {"job_id": "job-1"}


async def test_put_rip_record_reports_http_errors_without_raising():
    patcher, _ = _http_client(_resp(404, '{"detail":"Not Found"}'))
    with patcher:
        ok, error = await catalog_client.put_rip_record("job-1", {})
    assert ok is False and error.startswith("HTTP 404")


async def test_put_rip_record_reports_an_unreachable_library():
    patcher, _ = _http_client(error=httpx.ConnectError("connection refused"))
    with patcher:
        ok, error = await catalog_client.put_rip_record("job-1", {})
    assert ok is False and "ConnectError" in error


@pytest.mark.parametrize("response,error,expected", [
    (_resp(200), None, "found"),
    (_resp(404), None, "missing"),
    (_resp(500), None, "unreachable"),
    (None, httpx.ConnectError("down"), "unreachable"),
])
async def test_check_disc(response, error, expected):
    patcher, _ = _http_client(response, error)
    with patcher:
        assert await catalog_client.check_disc("disc-1") == expected


# ── Delivery ────────────────────────────────────────────────────────────────

async def test_delivery_registers_before_moving_and_confirms_after(make_job):
    job_id = make_job(status="queued", title="Pushed Movie", year=2015,
                      mkv_title_indices=[0], catalog_disc_id="disc-1")
    seen: list[tuple[str, bool]] = []

    async def put(pushed_id, manifest):
        from pathlib import Path
        seen.append((manifest["status"], Path(manifest["titles"][0]["dest_path"]).exists()))
        return True, None

    link = await _run(job_id, put)

    # First call: files announced but not there yet. Second: delivered.
    assert seen == [("delivering", False), ("delivered", True)]
    link.assert_not_called()      # the record carries the disc link; link-source is not needed
    job = _job(job_id)
    assert job.status == "done" and job.library_sync_state == "synced" and job.library_sync_error is None


async def test_library_down_does_not_fail_the_rip_and_falls_back_to_link_source(make_job):
    job_id = make_job(status="queued", title="Offline Movie", year=2016,
                      mkv_title_indices=[0], catalog_disc_id="disc-1")
    link = await _run(job_id, AsyncMock(return_value=(False, "ConnectError: refused")))

    job = _job(job_id)
    assert job.status == "done" and len(job.output_paths) == 1
    assert job.library_sync_state == "failed" and "refused" in job.library_sync_error
    link.assert_called_once()
    assert job_manager.unsynced_job_ids() == [job_id]


async def test_outbox_delivers_once_the_library_is_back(make_job):
    job_id = make_job(status="queued", title="Late Movie", year=2017,
                      mkv_title_indices=[0], catalog_disc_id="disc-1")
    await _run(job_id, AsyncMock(return_value=(False, "ConnectError: refused")))

    put = AsyncMock(return_value=(True, None))
    with patch("services.job_manager.catalog_client.put_rip_record", new=put):
        result = await job_manager.sync_all()

    assert result == {"synced": [job_id], "failed": []}
    sent_id, sent_manifest = put.call_args.args
    assert sent_id == job_id and sent_manifest["status"] == "delivered"
    assert _job(job_id).library_sync_state == "synced"
    assert job_manager.unsynced_job_ids() == []


async def test_sync_all_backfill_resends_synced_jobs_but_not_unfinished_ones(make_job):
    done = make_job(status="queued", title="Backfill Movie", year=2018, mkv_title_indices=[0])
    await _run(done, AsyncMock(return_value=(True, None)))
    make_job(status="failed", title="Never Delivered")

    put = AsyncMock(return_value=(True, None))
    with patch("services.job_manager.catalog_client.put_rip_record", new=put):
        assert await job_manager.sync_all() == {"synced": [], "failed": []}
        assert await job_manager.sync_all(include_synced=True) == {"synced": [done], "failed": []}


# ── API ─────────────────────────────────────────────────────────────────────

def test_sync_endpoints(client, make_job):
    assert client.post("/jobs/nope/sync").status_code == 404
    bare = make_job(status="done", title="No Manifest")
    assert client.post(f"/jobs/{bare}/sync").status_code == 400

    job_manager._write_manifest(bare, "delivered", None, None)
    with patch("services.job_manager.catalog_client.put_rip_record", new=AsyncMock(return_value=(True, None))):
        body = client.post(f"/jobs/{bare}/sync").json()
        assert body["synced"] is True and body["library_sync_state"] == "synced"
        assert client.post("/jobs/sync-all").json() == {"synced": [], "failed": []}
        assert client.post("/jobs/sync-all", params={"all": True}).json() == {"synced": [bare], "failed": []}


def test_rename_marks_the_record_for_resending(client, make_job, tmp_path):
    from db.models import RipTitle

    delivered = tmp_path / "Old.mkv"
    delivered.write_bytes(b"x")
    job_id = make_job(status="done", title="Renamed", output_paths=[str(delivered)],
                      library_sync_state="synced")
    with SessionLocal() as db:
        db.add(RipTitle(job_id=job_id, title_index=0, file_seq=0, status="delivered", dest_path=str(delivered)))
        db.commit()
    job_manager._write_manifest(job_id, "delivered", None, None)

    assert client.patch(f"/jobs/{job_id}/files/0", json={"name": "New.mkv"}).status_code == 200
    assert _job(job_id).library_sync_state == "pending"
    assert job_manager.unsynced_job_ids() == [job_id]


# ── A rip must name its catalogued disc ─────────────────────────────────────

@pytest.fixture
def disc_required(monkeypatch):
    monkeypatch.setattr(settings, "require_catalog_disc", True)


def test_start_without_a_disc_is_rejected(client, disc_required):
    resp = client.post("/jobs/start", json=MOVIE)
    assert resp.status_code == 400
    assert "catalog_disc_id is required" in resp.json()["detail"]


def test_start_with_an_unknown_disc_is_rejected(client, disc_required):
    with patch("api.job_operations.catalog_client.check_disc", new=AsyncMock(return_value="missing")):
        resp = client.post("/jobs/start", json={**MOVIE, "catalog_disc_id": "nope"})
    assert resp.status_code == 400 and "not a disc" in resp.json()["detail"]


def test_start_when_the_catalog_cannot_be_asked(client, disc_required):
    with patch("api.job_operations.catalog_client.check_disc", new=AsyncMock(return_value="unreachable")):
        resp = client.post("/jobs/start", json={**MOVIE, "catalog_disc_id": "disc-1"})
    assert resp.status_code == 503


def test_start_with_a_catalogued_disc(client, disc_required):
    with patch("api.job_operations.catalog_client.check_disc", new=AsyncMock(return_value="found")):
        resp = client.post("/jobs/start", json={**MOVIE, "catalog_disc_id": "disc-1"})
    assert resp.status_code == 200 and resp.json()["catalog_disc_id"] == "disc-1"


def test_requirement_off_keeps_the_old_behaviour(client):
    assert settings.require_catalog_disc is False
    assert client.post("/jobs/start", json=MOVIE).status_code == 200
    with patch("api.job_operations.catalog_client.check_disc", new=AsyncMock(return_value="unreachable")):
        assert client.post("/jobs/start", json={**MOVIE, "title": "Other", "catalog_disc_id": "d"}).status_code == 200
