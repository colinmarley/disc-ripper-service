from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from db.database import SessionLocal
from db.models import DiscScan
from services.makemkv_service import makemkvcon_available, scan_disc, scan_summary

router = APIRouter(prefix="/disc", tags=["Disc"])


@router.get("/info")
async def get_disc_info():
    """Scan the disc drive, store the full scan, and return disc identity plus
    the title list (durations, chapters, source playlist, every stream).

    `scan_id` identifies the stored scan; send it back in `POST /jobs/start`
    so the job is tied to exactly this disc.
    """
    return await scan_disc()


@router.get("/status")
async def get_disc_status():
    return {"makemkvcon_available": makemkvcon_available()}


@router.get("/scans")
async def list_scans(fingerprint: str | None = None, limit: int = 20):
    """Recent stored scans, newest first; `?fingerprint=` finds earlier scans
    of the same disc."""
    with SessionLocal() as db:
        q = db.query(DiscScan).order_by(DiscScan.created_at.desc())
        if fingerprint:
            q = q.filter(DiscScan.fingerprint == fingerprint)
        return [scan_summary(s) for s in q.limit(max(1, min(limit, 200))).all()]


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: str):
    """A stored scan: summary plus everything parsed from it."""
    with SessionLocal() as db:
        scan = db.get(DiscScan, scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")
        return {**scan_summary(scan), "parsed": scan.parsed}


@router.get("/scans/{scan_id}/raw", response_class=PlainTextResponse)
async def get_scan_raw(scan_id: str):
    """The untouched `makemkvcon -r info` output of a stored scan."""
    with SessionLocal() as db:
        scan = db.get(DiscScan, scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")
        return scan.raw_output or ""
