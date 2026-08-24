from fastapi import APIRouter, HTTPException, Query

from services import tmdb_service

router = APIRouter(prefix="/tmdb", tags=["TMDB"])


@router.get("/search")
async def search(
    query: str = Query(..., min_length=1),
    media_type: str = Query(default="movie"),
):
    if media_type not in ("movie", "show"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'show'")
    try:
        return await tmdb_service.search(query, media_type)
    except tmdb_service.TmdbNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB request failed: {exc}")


@router.get("/{media_type}/{tmdb_id}")
async def get_details(media_type: str, tmdb_id: int):
    if media_type not in ("movie", "show"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'show'")
    try:
        return await tmdb_service.get_details(media_type, tmdb_id)
    except tmdb_service.TmdbNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB request failed: {exc}")


@router.get("/show/{series_id}/season/{season_number}")
async def get_season(series_id: int, season_number: int):
    try:
        return await tmdb_service.get_season(series_id, season_number)
    except tmdb_service.TmdbNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB request failed: {exc}")
