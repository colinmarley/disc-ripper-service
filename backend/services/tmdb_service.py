"""
TMDB client used during rip configuration — lets the user search for and pick
the movie/show a disc belongs to (and, for shows, season/episode metadata)
before ripping, instead of typing title/year/imdb_id by hand.

Mirrors the endpoint shapes of my-media-manager's frontend TmdbService.ts so
the eventual UI component is familiar, but keeps the API key server-side
(TMDB_API_KEY env var) rather than exposing it to the browser.
"""

import httpx

from config.settings import settings
from homelab_logging import setup_logging, get_logger
from homelab_logging.config import LoggingConfig

# Idempotent: guards against import order (this module may be imported
# before main.py has had a chance to call setup_logging() itself).
setup_logging(LoggingConfig(project="disc-ripper-service", service="backend"))
logger = get_logger(__name__)

_TIMEOUT = 10.0


class TmdbNotConfigured(Exception):
    pass


def _require_api_key() -> str:
    if not settings.tmdb_api_key:
        raise TmdbNotConfigured("TMDB_API_KEY is not set")
    return settings.tmdb_api_key


async def search(query: str, media_type: str) -> list[dict]:
    """
    Search TMDB for a movie or show by title.

    media_type: "movie" | "show" (mapped to TMDB's "tv" search endpoint)
    """
    api_key = _require_api_key()
    endpoint = "search/movie" if media_type == "movie" else "search/tv"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{settings.tmdb_base_url}/{endpoint}",
            params={"api_key": api_key, "query": query},
        )
        response.raise_for_status()
        return response.json().get("results", [])


async def get_season(series_id: int, season_number: int) -> dict:
    """Season details including the episode list, for episode-code assignment."""
    api_key = _require_api_key()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{settings.tmdb_base_url}/tv/{series_id}/season/{season_number}",
            params={"api_key": api_key},
        )
        response.raise_for_status()
        return response.json()


async def get_details(media_type: str, tmdb_id: int) -> dict:
    """Full details for a single movie or show (title, year, overview, etc)."""
    api_key = _require_api_key()
    endpoint = "movie" if media_type == "movie" else "tv"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{settings.tmdb_base_url}/{endpoint}/{tmdb_id}",
            params={"api_key": api_key},
        )
        response.raise_for_status()
        return response.json()
