"""
Test configuration and shared fixtures.

The env vars are set at module import time so that application modules
(config/settings.py, db/database.py) pick up test values when first imported.
"""
import os
import tempfile

# Must come before any application imports
_tmpdir = tempfile.mkdtemp(prefix="ripper_test_")
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("LOGS_ROOT", os.path.join(_tmpdir, "logs"))
os.environ.setdefault("RIPS_ROOT", os.path.join(_tmpdir, "rips"))
os.environ.setdefault("INGEST_ROOT", os.path.join(_tmpdir, "ingest"))

import pytest
from sqlalchemy import delete


@pytest.fixture(scope="session", autouse=True)
def ensure_tables():
    """Create DB schema once for the test session."""
    from db.database import Base, engine
    from main import _migrate_db
    Base.metadata.create_all(bind=engine)
    _migrate_db()


@pytest.fixture(autouse=True)
def mock_run_worker():
    """Replace the background worker with a no-op so TestClient startup doesn't
    spawn real MakeMKV/HandBrake subprocesses during tests."""
    import asyncio
    from unittest.mock import patch
    from services import job_manager as jm_module

    async def _noop():
        await asyncio.sleep(1e9)

    with patch.object(jm_module.job_manager, "run_worker", _noop):
        yield


@pytest.fixture(autouse=True)
def clean_db(ensure_tables, mock_run_worker):
    """Truncate all job data and reset job_manager asyncio state before each test.

    The job_manager singleton has an asyncio.Event that binds to the first event
    loop that uses it. Resetting it here ensures each TestClient (which creates its
    own event loop) gets a fresh, unbound Event.
    """
    import asyncio
    from db.database import SessionLocal
    from db.models import JobAnalysis, RipJob
    from services.job_manager import job_manager

    job_manager._new_job_event = asyncio.Event()
    job_manager._active_proc = None
    job_manager._active_job_id = None
    job_manager._log_subscribers = {}
    job_manager._last_written_progress = {}

    with SessionLocal() as db:
        db.execute(delete(JobAnalysis))
        db.execute(delete(RipJob))
        db.commit()
    yield


@pytest.fixture
def client(clean_db):
    """Synchronous FastAPI test client with full lifespan.

    Depends on clean_db to guarantee job_manager is reset before the
    TestClient starts its event loop.
    """
    from main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_job():
    """Insert a RipJob directly into the DB at a given status."""
    from datetime import datetime
    from uuid import uuid4
    from db.database import SessionLocal
    from db.models import RipJob

    def _make(
        status: str = "done",
        media_type: str = "movie",
        disc_type: str = "dvd",
        title: str = "Test Movie",
        year: int = 2023,
        output_paths: list = None,
        mkv_title_indices: list = None,
        **kwargs,
    ) -> str:
        job_id = str(uuid4())
        with SessionLocal() as db:
            db.add(RipJob(
                id=job_id,
                disc_type=disc_type,
                media_type=media_type,
                title=title,
                year=year,
                status=status,
                mkv_title_indices=mkv_title_indices or [0],
                output_paths=output_paths or [],
                **kwargs,
            ))
            db.commit()
        return job_id

    return _make
