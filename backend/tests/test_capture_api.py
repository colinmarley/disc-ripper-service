"""API surface added for rip-time capture: stored scans, the richer start
request, per-title records and the manifest."""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine

from services import manifest as manifest_service
from services.makemkv_service import parse_scan, save_scan

FIXTURES = Path(__file__).parent / "fixtures" / "makemkv"

MOVIE = {"disc_type": "bluray", "media_type": "movie", "title": "Gone Girl", "year": 2014}


def _saved_scan(name: str = "bluray_movie_info.txt") -> dict:
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    return save_scan(raw, parse_scan(raw))


# ── Scans ───────────────────────────────────────────────────────────────────

def test_disc_info_stores_scan_and_returns_identity(client):
    raw = (FIXTURES / "dvd_tv_info.txt").read_text(encoding="utf-8")

    class _Proc:
        returncode = 0

        async def communicate(self):
            return raw.encode(), b""

    with patch("services.makemkv_service.makemkvcon_available", return_value=True), \
         patch("services.makemkv_service.asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc())):
        body = client.get("/disc/info").json()

    assert body["error"] is None
    assert len(body["titles"]) == 3
    assert body["disc"]["volume_label"] == "FIREFLY_D1"
    assert body["disc"]["fingerprint"]
    assert body["titles"][0]["streams"][1]["lang_code"] == "eng"

    stored = client.get(f"/disc/scans/{body['scan_id']}").json()
    assert stored["parsed"]["disc"]["name"] == "Firefly, Disc 1"
    assert client.get(f"/disc/scans/{body['scan_id']}/raw").text == raw


def test_scans_can_be_found_by_fingerprint(client):
    first = _saved_scan()
    again = _saved_scan()
    other = _saved_scan("dvd_tv_info.txt")
    assert first["fingerprint"] == again["fingerprint"] != other["fingerprint"]

    found = client.get("/disc/scans", params={"fingerprint": first["fingerprint"]}).json()
    assert {s["scan_id"] for s in found} == {first["scan_id"], again["scan_id"]}
    assert len(client.get("/disc/scans").json()) == 3


def test_unknown_scan_is_404(client):
    assert client.get("/disc/scans/nope").status_code == 404
    assert client.get("/disc/scans/nope/raw").status_code == 404


# ── Start job ───────────────────────────────────────────────────────────────

def test_start_job_stores_tmdb_id_and_declared_scan(client):
    scan = _saved_scan()
    body = client.post("/jobs/start", json={**MOVIE, "tmdb_id": 210577, "scan_id": scan["scan_id"]}).json()
    assert body["tmdb_id"] == 210577
    assert body["scan_id"] == scan["scan_id"]
    assert body["scan_link"] == "declared"
    assert body["fingerprint"] == scan["fingerprint"]


def test_start_job_without_scan_id_infers_latest_scan(client):
    _saved_scan("dvd_tv_info.txt")
    latest = _saved_scan()
    body = client.post("/jobs/start", json=MOVIE).json()
    assert body["scan_id"] == latest["scan_id"]
    assert body["scan_link"] == "inferred"


def test_start_job_without_any_scan(client):
    body = client.post("/jobs/start", json=MOVIE).json()
    assert body["scan_id"] is None and body["scan_link"] is None and body["fingerprint"] is None


def test_start_job_derives_legacy_maps_from_titles(client):
    body = client.post("/jobs/start", json={
        "disc_type": "dvd", "media_type": "show", "title": "Firefly", "year": 2002, "season": 1,
        "mkv_title_indices": [0, 1, 2],
        "titles": [
            {"title_index": 0, "season": 1, "episode": 1, "episode_end": 2,
             "tmdb_episode_id": 85395, "episode_name": "Serenity"},
            {"title_index": 1, "season": 1, "episode": 3, "episode_name": "The Train Job"},
            {"title_index": 2, "content_type": "trailer"},
        ],
    }).json()
    assert body["episode_map"] == {"0": "S01E01-E02 - Serenity", "1": "S01E03 - The Train Job"}
    assert body["title_content_types"] == {"2": "trailer"}
    assert body["title_declarations"][0]["tmdb_episode_id"] == 85395


def test_explicit_legacy_maps_win_over_derived(client):
    body = client.post("/jobs/start", json={
        "disc_type": "dvd", "media_type": "show", "title": "Firefly", "year": 2002,
        "episode_map": {"0": "S01E09"},
        "titles": [{"title_index": 0, "season": 1, "episode": 1}],
    }).json()
    assert body["episode_map"] == {"0": "S01E09"}


def test_retry_carries_rip_metadata(client, make_job):
    scan = _saved_scan()
    job_id = make_job(
        status="failed", tmdb_id=210577, scan_id=scan["scan_id"], scan_link="declared",
        title_declarations=[{"title_index": 0, "content_type": "main_feature"}],
    )
    body = client.post(f"/jobs/{job_id}/retry").json()
    assert body["tmdb_id"] == 210577
    assert body["scan_id"] == scan["scan_id"] and body["scan_link"] == "declared"
    assert body["title_declarations"] == [{"title_index": 0, "content_type": "main_feature"}]


# ── Titles / manifest ───────────────────────────────────────────────────────

def test_job_titles_and_manifest_endpoints(client, make_job, tmp_path):
    from db.database import SessionLocal
    from db.models import RipTitle
    from services.job_manager import job_manager

    delivered = tmp_path / "Old Name.mkv"
    delivered.write_bytes(b"data")
    job_id = make_job(status="done", title="Renamed Movie", output_paths=[str(delivered)])
    with SessionLocal() as db:
        db.add(RipTitle(
            job_id=job_id, title_index=3, file_seq=0, status="delivered",
            makemkv_filename="title_t03.mkv", dest_path=str(delivered), size_bytes=4, read_errors=1,
        ))
        db.commit()

    assert client.get(f"/jobs/{job_id}/manifest").status_code == 404
    job_manager._write_manifest(job_id, "delivered", None, None)

    titles = client.get(f"/jobs/{job_id}/titles").json()
    assert titles[0]["title_index"] == 3
    assert titles[0]["makemkv_filename"] == "title_t03.mkv"
    assert titles[0]["read_errors"] == 1

    manifest = client.get(f"/jobs/{job_id}/manifest").json()
    assert manifest["titles"][0]["dest_path"] == str(delivered)
    assert client.get(f"/jobs/{job_id}").json()["has_manifest"] is True

    # Renaming a delivered file keeps the per-title record and manifest in step
    resp = client.patch(f"/jobs/{job_id}/files/0", json={"name": "New Name.mkv"})
    assert resp.status_code == 200
    new_path = str(tmp_path / "New Name.mkv")
    assert client.get(f"/jobs/{job_id}/titles").json()[0]["dest_path"] == new_path
    assert manifest_service.read_stored_manifest(job_id)["titles"][0]["dest_path"] == new_path


def test_titles_and_manifest_404_for_unknown_job(client):
    assert client.get("/jobs/nope/titles").status_code == 404
    assert client.get("/jobs/nope/manifest").status_code == 404


# ── Migration ───────────────────────────────────────────────────────────────

def test_migrate_adds_capture_columns_to_an_old_database(tmp_path):
    """A database created before this change gains the new columns and
    tables without losing its jobs."""
    import main
    from db.database import Base

    db_file = tmp_path / "old.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE rip_jobs (
            id VARCHAR PRIMARY KEY, created_at DATETIME, updated_at DATETIME,
            disc_type VARCHAR, media_type VARCHAR, title VARCHAR, year INTEGER,
            imdb_id VARCHAR, season INTEGER, mkv_title_indices JSON, episode_map JSON,
            status VARCHAR, progress FLOAT, error TEXT, rip_dir VARCHAR,
            output_paths JSON, log TEXT
        )
    """)
    conn.execute(
        "INSERT INTO rip_jobs (id, disc_type, media_type, title, year, status) "
        "VALUES ('old-job', 'dvd', 'movie', 'Old Movie', 1999, 'done')"
    )
    conn.commit()
    conn.close()

    old_engine = create_engine(f"sqlite:///{db_file}")
    with patch.object(main, "engine", old_engine):
        Base.metadata.create_all(bind=old_engine)
        main._migrate_db()
        main._migrate_db()  # idempotent

    conn = sqlite3.connect(db_file)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(rip_jobs)")}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    title = conn.execute("SELECT title FROM rip_jobs WHERE id='old-job'").fetchone()[0]
    conn.close()
    old_engine.dispose()

    assert {"tmdb_id", "scan_id", "scan_link", "fingerprint", "title_declarations",
            "rip_started_at", "rip_finished_at", "delivered_at", "manifest_path",
            "library_sync_state", "library_sync_error", "catalog_disc_id"} <= columns
    assert {"disc_scans", "rip_titles"} <= tables
    assert title == "Old Movie"
