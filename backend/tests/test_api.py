"""Integration tests for all job API endpoints via FastAPI TestClient."""
import pytest
from db.database import SessionLocal
from db.models import RipJob


# ── health ────────────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── POST /jobs/start ──────────────────────────────────────────────────────────

def test_start_movie_job(client):
    resp = client.post("/jobs/start", json={
        "disc_type": "dvd",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "mkv_title_indices": [0],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Inception"
    assert data["year"] == 2010
    assert data["disc_type"] == "dvd"
    assert data["media_type"] == "movie"
    assert data["status"] == "queued"
    assert data["id"]


def test_start_show_job_with_episode_map(client):
    resp = client.post("/jobs/start", json={
        "disc_type": "bluray",
        "media_type": "show",
        "title": "The Wire",
        "year": 2002,
        "season": 1,
        "mkv_title_indices": [0, 1],
        "episode_map": {"0": "S01E01 - The Target", "1": "S01E02 - The Detail"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["season"] == 1
    assert data["episode_map"]["0"] == "S01E01 - The Target"
    assert data["episode_map"]["1"] == "S01E02 - The Detail"


def test_start_job_stores_imdb_id(client):
    resp = client.post("/jobs/start", json={
        "disc_type": "dvd",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "imdb_id": "tt1375666",
        "mkv_title_indices": [0],
    })
    assert resp.status_code == 200
    assert resp.json()["imdb_id"] == "tt1375666"


def test_start_job_invalid_disc_type(client):
    resp = client.post("/jobs/start", json={
        "disc_type": "vhs",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "mkv_title_indices": [0],
    })
    assert resp.status_code == 400


def test_start_job_invalid_media_type(client):
    resp = client.post("/jobs/start", json={
        "disc_type": "dvd",
        "media_type": "podcast",
        "title": "Inception",
        "year": 2010,
        "mkv_title_indices": [0],
    })
    assert resp.status_code == 400


def test_start_job_empty_indices(client):
    resp = client.post("/jobs/start", json={
        "disc_type": "dvd",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "mkv_title_indices": [],
    })
    assert resp.status_code == 400


def test_start_job_duplicate_active_rejected(client, make_job):
    make_job(status="ripping", title="Inception", year=2010, disc_type="dvd")
    resp = client.post("/jobs/start", json={
        "disc_type": "dvd",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "mkv_title_indices": [0],
    })
    assert resp.status_code == 409
    assert "already active" in resp.json()["detail"]


def test_start_job_duplicate_allowed_after_done(client, make_job):
    # A completed job does not block a new one for the same title
    make_job(status="done", title="Inception", year=2010, disc_type="dvd")
    resp = client.post("/jobs/start", json={
        "disc_type": "dvd",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "mkv_title_indices": [0],
    })
    assert resp.status_code == 200


def test_start_job_with_catalog_disc_id_and_content_types(client):
    resp = client.post("/jobs/start", json={
        "disc_type": "dvd",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "mkv_title_indices": [0, 1],
        "catalog_disc_id": "disc-abc123",
        "title_content_types": {"1": "trailer"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["catalog_disc_id"] == "disc-abc123"
    assert data["title_content_types"] == {"1": "trailer"}


# ── GET /jobs ─────────────────────────────────────────────────────────────────

def test_list_jobs_empty(client):
    assert client.get("/jobs").json() == []


def test_list_jobs_returns_all(client, make_job):
    make_job(title="Movie A")
    make_job(title="Movie B")
    jobs = client.get("/jobs").json()
    assert len(jobs) == 2
    titles = {j["title"] for j in jobs}
    assert titles == {"Movie A", "Movie B"}


def test_list_jobs_status_filter(client, make_job):
    make_job(status="done", title="Done Movie")
    make_job(status="failed", title="Failed Movie")
    done = client.get("/jobs?status=done").json()
    assert len(done) == 1
    assert done[0]["status"] == "done"


def test_list_jobs_ordered_by_created_desc(client, make_job):
    make_job(title="First")
    make_job(title="Second")
    jobs = client.get("/jobs").json()
    # Most recent first (created_at DESC)
    assert jobs[0]["title"] == "Second"


# ── GET /jobs/{id} ────────────────────────────────────────────────────────────

def test_get_job(client, make_job):
    job_id = make_job(title="Inception", year=2010)
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Inception"
    assert resp.json()["id"] == job_id


def test_get_job_not_found(client):
    assert client.get("/jobs/does-not-exist").status_code == 404


# ── POST /jobs/{id}/stop ──────────────────────────────────────────────────────

def test_stop_queued_job(client, make_job):
    job_id = make_job(status="queued")
    resp = client.post(f"/jobs/{job_id}/stop")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True


def test_stop_done_job_returns_not_found(client, make_job):
    job_id = make_job(status="done")
    assert client.post(f"/jobs/{job_id}/stop").status_code == 404


def test_stop_nonexistent_job(client):
    assert client.post("/jobs/does-not-exist/stop").status_code == 404


# ── POST /jobs/{id}/retry ─────────────────────────────────────────────────────

def test_retry_failed_job_creates_new_queued(client, make_job):
    job_id = make_job(status="failed", title="Inception", year=2010, disc_type="dvd")
    resp = client.post(f"/jobs/{job_id}/retry")
    assert resp.status_code == 200
    new = resp.json()
    assert new["id"] != job_id
    assert new["status"] == "queued"
    assert new["title"] == "Inception"
    assert new["year"] == 2010


def test_retry_cancelled_job(client, make_job):
    job_id = make_job(status="cancelled")
    resp = client.post(f"/jobs/{job_id}/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


def test_retry_running_job_rejected(client, make_job):
    job_id = make_job(status="ripping")
    assert client.post(f"/jobs/{job_id}/retry").status_code == 400


def test_retry_not_found(client):
    assert client.post("/jobs/does-not-exist/retry").status_code == 404


def test_retry_preserves_episode_map(client, make_job):
    ep_map = {"0": "S01E01 - Pilot", "1": "S01E02 - Second"}
    job_id = make_job(
        status="failed",
        media_type="show",
        episode_map=ep_map,
        season=1,
        mkv_title_indices=[0, 1],
    )
    resp = client.post(f"/jobs/{job_id}/retry")
    assert resp.status_code == 200
    assert resp.json()["episode_map"] == ep_map


# ── PATCH /jobs/{id}/files/{n} ────────────────────────────────────────────────

def test_rename_file_success(client, make_job, tmp_path):
    src = tmp_path / "Inception (2010).mkv"
    src.write_bytes(b"fake mkv")
    job_id = make_job(status="done", output_paths=[str(src)])

    new_name = "Inception (2010) - Director Cut.mkv"
    resp = client.patch(f"/jobs/{job_id}/files/0", json={"name": new_name})
    assert resp.status_code == 200
    assert (tmp_path / new_name).exists()
    assert not src.exists()
    assert resp.json()["output_paths"][0].endswith(new_name)


def test_rename_no_mkv_extension(client, make_job, tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"")
    job_id = make_job(status="done", output_paths=[str(src)])
    assert client.patch(f"/jobs/{job_id}/files/0", json={"name": "movie.avi"}).status_code == 400


def test_rename_path_traversal_slash(client, make_job, tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"")
    job_id = make_job(status="done", output_paths=[str(src)])
    assert client.patch(f"/jobs/{job_id}/files/0", json={"name": "../../passwd.mkv"}).status_code == 400


def test_rename_path_traversal_dotdot(client, make_job, tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"")
    job_id = make_job(status="done", output_paths=[str(src)])
    assert client.patch(f"/jobs/{job_id}/files/0", json={"name": "..safe.mkv"}).status_code == 400


def test_rename_non_done_job(client, make_job, tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"")
    job_id = make_job(status="ripping", output_paths=[str(src)])
    assert client.patch(f"/jobs/{job_id}/files/0", json={"name": "new.mkv"}).status_code == 400


def test_rename_index_out_of_range(client, make_job, tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"")
    job_id = make_job(status="done", output_paths=[str(src)])
    assert client.patch(f"/jobs/{job_id}/files/99", json={"name": "new.mkv"}).status_code == 404


def test_rename_conflict_with_existing_file(client, make_job, tmp_path):
    src = tmp_path / "movie.mkv"
    conflict = tmp_path / "existing.mkv"
    src.write_bytes(b"")
    conflict.write_bytes(b"")
    job_id = make_job(status="done", output_paths=[str(src)])
    assert client.patch(f"/jobs/{job_id}/files/0", json={"name": "existing.mkv"}).status_code == 409


def test_rename_same_name_is_noop(client, make_job, tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"")
    job_id = make_job(status="done", output_paths=[str(src)])
    resp = client.patch(f"/jobs/{job_id}/files/0", json={"name": "movie.mkv"})
    assert resp.status_code == 200
    assert src.exists()


# ── GET /jobs/{id}/log ────────────────────────────────────────────────────────

def test_log_done_job_returns_sse_burst(client, make_job):
    job_id = make_job(status="done")
    with SessionLocal() as db:
        j = db.get(RipJob, job_id)
        j.log = "line one\nline two"
        db.commit()

    resp = client.get(f"/jobs/{job_id}/log")
    assert resp.status_code == 200
    content = resp.text
    assert "data: line one" in content
    assert "data: line two" in content
    assert "data: [EOF]" in content


def test_log_not_found(client):
    assert client.get("/jobs/does-not-exist/log").status_code == 404


# ── GET /jobs/{id}/analysis ───────────────────────────────────────────────────

def test_get_analysis_no_analysis(client, make_job):
    job_id = make_job(status="failed")
    assert client.get(f"/jobs/{job_id}/analysis").status_code == 404


def test_get_analysis_nonexistent_job(client):
    assert client.get("/jobs/does-not-exist/analysis").status_code == 404


# ── startup recovery ──────────────────────────────────────────────────────────

def test_recover_stale_jobs(make_job):
    from services.job_manager import job_manager
    stale_ids = [
        make_job(status="ripping"),
        make_job(status="encoding"),
        make_job(status="delivering"),
    ]
    recovered = job_manager.recover_stale_jobs()
    assert recovered == 3

    with SessionLocal() as db:
        for jid in stale_ids:
            j = db.get(RipJob, jid)
            assert j.status == "failed"
            assert "restarted" in (j.error or "").lower()
