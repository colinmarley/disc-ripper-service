import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from db.database import SessionLocal
from db.models import JobAnalysis, RipJob
from services import catalog_client
from services import manifest as manifest_service
from services.analysis_service import analyze_failed_job, get_job_analysis
from config.settings import settings
from services.job_manager import job_manager
from homelab_logging import setup_logging, get_logger
from homelab_logging.config import LoggingConfig

# Idempotent: guards against import order (this module may be imported
# before main.py has had a chance to call setup_logging() itself).
setup_logging(LoggingConfig(project="disc-ripper-service", service="backend"))
logger = get_logger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


class RenameFileRequest(BaseModel):
    name: str


class TitleDeclaration(BaseModel):
    """What one selected disc title is, as declared when the rip is set up."""
    title_index: int
    # "main_feature" | "episode" | an extras category slug ("trailer", ...)
    content_type: Optional[str] = None
    is_main_feature: Optional[bool] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_end: Optional[int] = None       # last episode of a multi-episode title
    tmdb_episode_id: Optional[int] = None
    episode_name: Optional[str] = None
    custom_name: Optional[str] = None       # free-text name instead of an episode code


class StartJobRequest(BaseModel):
    disc_type: str                      # "dvd" | "bluray"
    media_type: str                     # "movie" | "show"
    title: str
    year: int
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None       # TMDB movie id or series id
    season: Optional[int] = None
    mkv_title_indices: list[int] = [0]
    episode_map: Optional[dict[str, str]] = None  # {"0": "S01E01", "1": "S01E02"}
    catalog_disc_id: Optional[str] = None  # links this rip to a my-media-manager Disc record
    title_content_types: Optional[dict[str, str]] = None  # {"0": "trailer", "2": "deleted_scene"}
    scan_id: Optional[str] = None       # from GET /disc/info — ties the job to that scan
    # Structured per-title declarations. When given, episode_map and
    # title_content_types are derived from it unless they are also sent.
    titles: Optional[list[TitleDeclaration]] = None


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
        "catalog_disc_id": job.catalog_disc_id,
        "title_content_types": job.title_content_types,
        "tmdb_id": job.tmdb_id,
        "scan_id": job.scan_id,
        "scan_link": job.scan_link,
        "fingerprint": job.fingerprint,
        "title_declarations": job.title_declarations,
        "rip_started_at": job.rip_started_at.isoformat() if job.rip_started_at else None,
        "rip_finished_at": job.rip_finished_at.isoformat() if job.rip_finished_at else None,
        "delivered_at": job.delivered_at.isoformat() if job.delivered_at else None,
        "has_manifest": bool(job.manifest_path),
        "library_sync_state": job.library_sync_state,
        "library_sync_error": job.library_sync_error,
    }


def _rip_title_dict(row) -> dict:
    return {
        "title_index": row.title_index,
        "file_seq": row.file_seq,
        "status": row.status,
        "error": row.error,
        "makemkv_filename": row.makemkv_filename,
        "dest_path": row.dest_path,
        "size_bytes": row.size_bytes,
        "sha256": row.sha256,
        "read_errors": row.read_errors or 0,
        "messages": row.messages or [],
        "rip_started_at": row.rip_started_at.isoformat() if row.rip_started_at else None,
        "rip_finished_at": row.rip_finished_at.isoformat() if row.rip_finished_at else None,
        "duration_seconds": (row.probe or {}).get("duration_seconds"),
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

    if settings.require_catalog_disc and not req.catalog_disc_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "catalog_disc_id is required: link the rip to a disc in the my-media-manager "
                "catalog (its Rip Disc page creates one), or set REQUIRE_CATALOG_DISC=false"
            ),
        )

    if req.catalog_disc_id:
        found = await catalog_client.check_disc(req.catalog_disc_id)
        if found == "missing" and settings.require_catalog_disc:
            raise HTTPException(
                status_code=400,
                detail=f"catalog_disc_id '{req.catalog_disc_id}' is not a disc in the my-media-manager catalog",
            )
        if found == "unreachable" and settings.require_catalog_disc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not reach the my-media-manager catalog to check the disc "
                    f"({settings.media_manager_api_url}). Try again, or set REQUIRE_CATALOG_DISC=false"
                ),
            )
        if found != "found":
            logger.warning("catalog_disc_id_unverified", catalog_disc_id=req.catalog_disc_id, result=found)

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
        "catalog_disc_id": original.catalog_disc_id,
        "title_content_types": original.title_content_types,
        "tmdb_id": original.tmdb_id,
        # Only a scan the user explicitly chose is carried over; an inferred
        # one is re-inferred (the disc may have been swapped since).
        "scan_id": original.scan_id if original.scan_link == "declared" else None,
        "title_declarations": original.title_declarations,
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

    # The per-title record and the manifest are keyed by path: keep them in step.
    job_manager.rename_delivered_file(job_id, str(old_path), str(new_path))
    return _job_dict(job_manager.get_job(job_id))


@router.post("/sync-all")
async def sync_all_jobs(all: bool = False):
    """Send rip records my-media-manager has not taken yet. `?all=true` sends
    every delivered job's record again (a backfill; the library ignores repeats)."""
    return await job_manager.sync_all(include_synced=all)


@router.post("/{job_id}/sync")
async def sync_job(job_id: str):
    """Send one job's rip record to my-media-manager now."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.manifest_path:
        raise HTTPException(status_code=400, detail="This job has no manifest to send")
    ok = await job_manager.sync_library(job_id)
    return _job_dict(job_manager.get_job(job_id)) | {"synced": ok}


@router.get("/{job_id}/titles")
async def get_job_titles(job_id: str):
    """Per-file records of a job: which disc title each file came from, its
    size, checksum, read errors and delivered path."""
    if not job_manager.get_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return [_rip_title_dict(r) for r in job_manager.rip_titles(job_id)]


@router.get("/{job_id}/manifest")
async def get_job_manifest(job_id: str):
    """The rip manifest as delivered with the files (the ripper's own copy)."""
    if not job_manager.get_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    manifest = manifest_service.read_stored_manifest(job_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="No manifest for this job")
    return manifest


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
