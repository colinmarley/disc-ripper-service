from fastapi import APIRouter
from services.makemkv_service import scan_disc, makemkvcon_available

router = APIRouter(prefix="/disc", tags=["Disc"])


@router.get("/info")
async def get_disc_info():
    """Scan the disc drive and return title list with durations and stream info."""
    return await scan_disc()


@router.get("/status")
async def get_disc_status():
    return {"makemkvcon_available": makemkvcon_available()}
