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
from homelab_logging import setup_logging, get_logger
from homelab_logging.config import LoggingConfig

# Idempotent: this module may be imported directly (e.g. by tests, or via
# job_manager.py's import) before main.py has had a chance to call
# setup_logging() itself.
setup_logging(LoggingConfig(project="disc-ripper-service", service="backend"))
logger = get_logger(__name__)

_TIMEOUT = 10.0
# A manifest carries every title and stream of the disc; give the store time.
_RIP_RECORD_TIMEOUT = 30.0


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


async def link_delivered_files(file_paths: list[str], disc_id: str) -> bool:
    """
    Tell my-media-manager which disc a set of just-delivered files came from.

    This is the only point where that link is ever communicated —
    _run_deliver() moves files into the shared ingest bind mount with
    no other push to my-media-manager, so if this call doesn't happen (or
    fails), the files land and get ingested normally but with no disc_id set.
    Best-effort: logs and returns False on failure rather than raising, since
    a catalog-link failure shouldn't be treated as a failed rip job.
    """
    url = f"{settings.media_manager_api_url}/api/catalog/link-source"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json={"filePaths": file_paths, "discId": disc_id})
            response.raise_for_status()
            return True
    except httpx.HTTPError as exc:
        logger.warning("catalog_link_source_failed", disc_id=disc_id, error=str(exc))
        return False


async def check_disc(disc_id: str) -> str:
    """Whether a catalog disc exists: "found", "missing" or "unreachable".

    Unlike get_disc this tells a disc that is not in the catalog apart from
    a catalog that could not be asked.
    """
    url = f"{settings.media_manager_api_url}/api/catalog/discs/{disc_id}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("catalog_check_disc_failed", disc_id=disc_id, error=str(exc))
        return "unreachable"
    if response.status_code == 404:
        return "missing"
    if response.status_code >= 400:
        logger.warning("catalog_check_disc_failed", disc_id=disc_id, status=response.status_code)
        return "unreachable"
    return "found"


async def put_rip_record(job_id: str, manifest: dict) -> tuple[bool, str | None]:
    """Send a rip's manifest to my-media-manager (`PUT /api/rips/{job_id}`).

    The endpoint is idempotent on the job id, so this is safe to repeat.
    Returns (ok, error); never raises, because a library that is down must
    not fail a rip — the job keeps the record and tries again later.
    """
    url = f"{settings.media_manager_api_url}/api/rips/{job_id}"
    try:
        async with httpx.AsyncClient(timeout=_RIP_RECORD_TIMEOUT) as client:
            response = await client.put(url, json=manifest)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if response.status_code >= 400:
        return False, f"HTTP {response.status_code}: {response.text[:300]}"
    return True, None
