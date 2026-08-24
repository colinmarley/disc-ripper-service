"""
Tests for JobManager._run_deliver's catalog-disc linking step.

Delivery is filesystem-only (no other push from disc-ripper-service to
my-media-manager), so this call to catalog_client.link_delivered_files is the
only point where a rip job's catalog_disc_id ever reaches the other service.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import settings
from services.job_manager import job_manager


async def _null_log(_line: str):
    pass


def _make_rip_dir(job_id: str, filenames: list[str]) -> str:
    rip_dir = os.path.join(settings.rips_root, "movies", "test", job_id[:8])
    os.makedirs(rip_dir, exist_ok=True)
    for name in filenames:
        Path(rip_dir, name).write_bytes(b"fake-mkv-data")
    return rip_dir


@pytest.mark.asyncio
async def test_deliver_links_files_when_catalog_disc_id_set(make_job):
    job_id = make_job(
        status="ripping",
        title="Linked Movie",
        year=2020,
        mkv_title_indices=[0],
        catalog_disc_id="disc-xyz",
    )
    job_manager._update_field(job_id, rip_dir=_make_rip_dir(job_id, ["rip_0000.mkv"]))

    with patch(
        "services.job_manager.catalog_client.link_delivered_files",
        new=AsyncMock(return_value=True),
    ) as mock_link:
        await job_manager._run_deliver(job_id, _null_log)

    mock_link.assert_called_once()
    called_paths, called_disc_id = mock_link.call_args[0]
    assert called_disc_id == "disc-xyz"
    assert len(called_paths) == 1
    assert called_paths[0].endswith(".mkv")


@pytest.mark.asyncio
async def test_deliver_skips_linking_when_no_catalog_disc_id(make_job):
    job_id = make_job(status="ripping", title="Unlinked Movie", year=2021, mkv_title_indices=[0])
    job_manager._update_field(job_id, rip_dir=_make_rip_dir(job_id, ["rip_0000.mkv"]))

    with patch(
        "services.job_manager.catalog_client.link_delivered_files",
        new=AsyncMock(return_value=True),
    ) as mock_link:
        await job_manager._run_deliver(job_id, _null_log)

    mock_link.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_does_not_fail_job_when_link_unreachable(make_job):
    """A catalog-link failure is logged but must not fail the whole rip job."""
    job_id = make_job(
        status="ripping",
        title="Best Effort Movie",
        year=2022,
        mkv_title_indices=[0],
        catalog_disc_id="disc-unreachable",
    )
    job_manager._update_field(job_id, rip_dir=_make_rip_dir(job_id, ["rip_0000.mkv"]))

    with patch(
        "services.job_manager.catalog_client.link_delivered_files",
        new=AsyncMock(return_value=False),
    ):
        await job_manager._run_deliver(job_id, _null_log)

    from db.database import SessionLocal
    from db.models import RipJob

    with SessionLocal() as db:
        job = db.get(RipJob, job_id)
        assert job.status == "done"
