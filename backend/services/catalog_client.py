"""
Thin client for my-media-manager's disc catalog API.

my-media-manager's Postgres `discs` table is the canonical record of physical
discs owned — this service never persists disc identity itself, only a
`catalog_disc_id` pointer on RipJob (see db/models.py). This client is used
both by the rip-configuration flow (search/create a disc before starting a
job) and to fetch a disc's details for display.
"""

import httpx

from config.settings import settings
from homelab_logging import get_logger

logger = get_logger(__name__)

_TIMEOUT = 10.0


async def search_discs(title: str | None = None, barcode: str | None = None) -> list[dict]:
    """Look up existing catalog discs by title and/or barcode."""
    if not title and not barcode:
        return []

    params: dict[str, str] = {}
    if title:
        params["title"] = title
    if barcode:
        params["barcode"] = barcode

    url = f"{settings.media_manager_api_url}/api/catalog/discs/search"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.warning("catalog_search_failed", error=str(exc))
        return []


async def get_disc(disc_id: str) -> dict | None:
    """Fetch a single disc's catalog record, or None if not found/unreachable."""
    url = f"{settings.media_manager_api_url}/api/catalog/discs/{disc_id}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.warning("catalog_get_disc_failed", disc_id=disc_id, error=str(exc))
        return None


async def create_disc(body: dict) -> dict:
    """
    Create a new catalog disc record. Raises httpx.HTTPStatusError on failure —
    unlike search/get, callers need to know if a create they explicitly
    requested actually succeeded.
    """
    url = f"{settings.media_manager_api_url}/api/catalog/discs"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(url, json=body)
        response.raise_for_status()
        return response.json()
