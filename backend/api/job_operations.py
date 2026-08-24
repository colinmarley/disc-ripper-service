import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from db.database import SessionLocal
from db.models import JobAnalysis, RipJob
from services.analysis_service import analyze_failed_job, get_job_analysis
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
    dvd_quality: Optional[int] = None   # CRF quality (16-28); None = use global default
    dvd_encoder: Optional[str] = None   # e.g. "nvenc_h265", "x265", "x264"


def _analysis_dict(a: JobAnalysis) -> dict:
    return {
        "id": a.id,
        "job_id": a.job_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "error_type": a.error_type,
        "error_summary": a.error_summary,
        "suggested_fix": a.suggested_fix,
        "claude_prompt": a.claude_prompt,
        "full_analysis": a.full_analysis,
        "model_used": a.model_used,
        "log_path": a.log_path,
    }


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
        "encode_quality": job.encode_quality,
        "encode_encoder": job.encode_encoder,
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

    with SessionLocal() as db:
        dupe = (
            db.query(RipJob)
            .filter(
                RipJob.title == req.title,
                RipJob.year == req.year,
                RipJob.disc_type == req.disc_type,
                RipJob.status.in_(("queued", "ripping", "encoding", "delivering")),
            )
            .first()
        )
    if dupe:
        raise HTTPException(
            status_code=409,
            detail=f"A job for '{req.title} ({req.year})' is already active (id: {dupe.id})",
        )

    job = job_manager.create_job(req.model_dump())
    return _job_dict(job)


@router.post("/{job_id}/retry")
async def retry_job(job_id: str):
    """Clone a failed or cancelled job's config into a new queued job."""
    original = job_manager.get_job(job_id)
    if not original:
        raise HTTPException(status_code=404, detail="Job not found")
    if original.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=400, detail="Can only retry failed or cancelled jobs")

    new_job = job_manager.create_job({
        "disc_type": original.disc_type,
        "media_type": original.media_type,
        "title": original.title,
        "year": original.year,
        "imdb_id": original.imdb_id,
        "season": original.season,
        "mkv_title_indices": original.mkv_title_indices,
        "episode_map": original.episode_map,
        "dvd_quality": original.encode_quality,
        "dvd_encoder": original.encode_encoder,
    })
    return _job_dict(new_job)


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


@router.post("/{job_id}/analyze")
async def analyze_job(job_id: str):
    """
    Trigger AI failure analysis for a failed job via Ollama.
    Takes 30-90 seconds depending on model and log size.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "failed":
        raise HTTPException(status_code=400, detail="Can only analyze failed jobs")

    try:
        analysis = await analyze_failed_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama analysis failed: {e}")

    return _analysis_dict(analysis)


@router.get("/{job_id}/analysis")
async def get_analysis(job_id: str):
    """Return the saved analysis for a job, 404 if none exists."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    analysis = get_job_analysis(job_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="No analysis found for this job")

    return _analysis_dict(analysis)
