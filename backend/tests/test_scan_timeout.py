"""A scan that does not finish must not leave makemkvcon holding the drive.

A scratched disc can hang makemkvcon. The scan used to give up after its
timeout but leave the process running: it kept the drive, and every later
scan queued behind it and timed out too.
"""

import asyncio
import sys
from unittest.mock import patch

import pytest

from config.settings import settings
from services import makemkv_service

REAL_EXEC = asyncio.create_subprocess_exec


def _hanging_makemkvcon(started: list):
    """Stands in for create_subprocess_exec: starts a real process that never answers."""

    async def _exec(*_args, **kwargs):
        proc = await REAL_EXEC(sys.executable, "-c", "import time; time.sleep(120)", **kwargs)
        started.append(proc)
        return proc

    return _exec


@pytest.fixture
def hanging(monkeypatch):
    started: list = []
    monkeypatch.setattr(makemkv_service, "makemkvcon_available", lambda: True)
    monkeypatch.setattr(makemkv_service.asyncio, "create_subprocess_exec", _hanging_makemkvcon(started))
    yield started
    for proc in started:  # never leave a sleeper behind, whatever the test did
        if proc.returncode is None:
            proc.kill()


async def test_a_timed_out_scan_stops_makemkvcon(hanging, monkeypatch):
    monkeypatch.setattr(settings, "scan_timeout_seconds", 0.5)

    result = await makemkv_service.scan_disc()

    assert result["titles"] == []
    assert result["error"].startswith("Disc scan timed out after 0.5s")
    assert "drive is free" in result["error"]
    assert len(hanging) == 1 and hanging[0].returncode is not None


async def test_a_cancelled_scan_stops_makemkvcon(hanging, monkeypatch):
    monkeypatch.setattr(settings, "scan_timeout_seconds", 60)

    task = asyncio.create_task(makemkv_service.scan_disc())
    while not hanging:
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert hanging[0].returncode is not None


async def test_scans_run_one_at_a_time(hanging, monkeypatch):
    monkeypatch.setattr(settings, "scan_timeout_seconds", 0.5)

    first, second = await asyncio.gather(makemkv_service.scan_disc(), makemkv_service.scan_disc())

    assert "timed out" in first["error"] and "timed out" in second["error"]
    assert len(hanging) == 2
    # The second makemkvcon only started once the first had been stopped.
    assert all(proc.returncode is not None for proc in hanging)


async def test_a_process_that_ignores_sigterm_is_killed(monkeypatch):
    monkeypatch.setattr(makemkv_service, "STOP_GRACE_SECONDS", 0.2)
    proc = await REAL_EXEC(sys.executable, "-c", "import time; time.sleep(120)")
    with patch.object(proc, "terminate", lambda: None):  # behaves as if SIGTERM were ignored
        await makemkv_service._stop_process(proc)
    assert proc.returncode is not None
