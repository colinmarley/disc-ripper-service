import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from db.database import SessionLocal
from db.models import RipJob
from services.job_manager import job_manager

router = APIRouter(prefix="/jobs", tags=["Jobs"])


class RenameFileRequest(BaseModel):
    name: str


class StartJobRequest(BaseModel):
    disc_type: str                      # "dvd" | "bluray"
    media_type: str                     # "movie" | "show"
    title: str
    year: int
    imdb_id: Optional[str] = None
    season: Optional[int] = None
    mkv_title_indices: list[int] = [0]
    episode_map: Optional[dict[str, str]] = None  # {"0": "S01E01", "1": "S01E02"}


def _job_dict(job) -> dict:
    return {
        "id": job.id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "disc_type": job.disc_type,
        "media_type": job.media_type,
        "title": job.title,
        "year": job.year,
        "imdb_id": job.imdb_id,
        "season": job.season,
        "mkv_title_indices": job.mkv_title_indices,
        "episode_map": job.episode_map,
        "status": job.status,
        "progress": job.progress,
        "error": job.error,
        "rip_dir": job.rip_dir,
        "output_paths": job.output_paths,
    }


@router.post("/start")
async def start_job(req: StartJobRequest):
    """Create and enqueue a new rip job."""
    if req.disc_type not in ("dvd", "bluray"):
        raise HTTPException(status_code=400, detail="disc_type must be 'dvd' or 'bluray'")
    if req.media_type not in ("movie", "show"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'show'")
    if not req.mkv_title_indices:
        raise HTTPException(status_code=400, detail="mkv_title_indices must not be empty")

    job = job_manager.create_job(req.model_dump())
    return _job_dict(job)


@router.post("/{job_id}/stop")
async def stop_job(job_id: str):
    """Cancel a running or queued job."""
    ok = job_manager.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or already finished")
    return {"cancelled": True}


@router.get("")
async def list_jobs(status: Optional[str] = None):
    """List jobs, optionally filtered by status."""
    jobs = job_manager.list_jobs(status=status)
    return [_job_dict(j) for j in jobs]


@router.get("/{job_id}")
async def get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_dict(job)


@router.get("/{job_id}/files/{file_index}/stream")
async def stream_file(job_id: str, file_index: int):
    """Stream a delivered output file for in-browser preview."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if file_index < 0 or file_index >= len(job.output_paths):
        raise HTTPException(status_code=404, detail="File index out of range")
    path = job.output_paths[file_index]
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(path, media_type="video/x-matroska")


@router.patch("/{job_id}/files/{file_index}")
async def rename_file(job_id: str, file_index: int, body: RenameFileRequest):
    """Rename a delivered output file on disk and update the job record."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "done":
        raise HTTPException(status_code=400, detail="Can only rename files from completed jobs")
    if file_index < 0 or file_index >= len(job.output_paths):
        raise HTTPException(status_code=404, detail="File index out of range")

    new_name = body.name
    if not new_name.endswith(".mkv"):
        raise HTTPException(status_code=400, detail="Filename must end in .mkv")
    if any(c in new_name for c in ('/', '\\')) or '..' in new_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    old_path = Path(job.output_paths[file_index])
    if not old_path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")

    new_path = old_path.parent / new_name
    if new_path == old_path:
        return _job_dict(job)
    if new_path.exists():
        raise HTTPException(status_code=409, detail="A file with that name already exists")

    os.rename(old_path, new_path)

    with SessionLocal() as db:
        j = db.get(RipJob, job_id)
        paths = list(j.output_paths)
        paths[file_index] = str(new_path)
        j.output_paths = paths
        db.commit()
        db.refresh(j)
        return _job_dict(j)


@router.get("/{job_id}/log")
async def stream_log(job_id: str):
    """
    Server-Sent Events stream of live log lines for a running job.
    Returns stored log for completed jobs.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # For finished jobs, return stored log as a single SSE burst
    if job.status in ("done", "failed", "cancelled"):
        async def stored():
            for line in (job.log or "").splitlines():
                yield f"data: {line}\n\n"
            yield "data: [EOF]\n\n"
        return StreamingResponse(stored(), media_type="text/event-stream")

    # For active/queued jobs, subscribe to live log and stream
    q = job_manager.subscribe_log(job_id)

    async def live():
        # First send any already-stored log
        stored_log = (job_manager.get_job(job_id) or job).log or ""
        for line in stored_log.splitlines():
            yield f"data: {line}\n\n"

        try:
            while True:
                try:
                    line = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if line is None:
                    yield "data: [EOF]\n\n"
                    break
                yield f"data: {line}\n\n"
        finally:
            job_manager.unsubscribe_log(job_id, q)

    return StreamingResponse(live(), media_type="text/event-stream")
