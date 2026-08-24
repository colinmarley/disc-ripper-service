"""Tests for services/catalog_client.py — mocked httpx, no real network calls."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services import catalog_client


def _response(status_code=200, json_data=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def _mock_client(get_return=None, post_return=None, raise_on_request=None):
    client = AsyncMock()
    if raise_on_request:
        client.get.side_effect = raise_on_request
        client.post.side_effect = raise_on_request
    else:
        client.get.return_value = get_return
        client.post.return_value = post_return
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


# ── search_discs ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_discs_returns_empty_without_query():
    result = await catalog_client.search_discs()
    assert result == []


@pytest.mark.asyncio
async def test_search_discs_returns_results():
    client = _mock_client(get_return=_response(json_data=[{"id": "d1", "title": "Inception"}]))
    with patch("httpx.AsyncClient", return_value=client):
        result = await catalog_client.search_discs(title="Inception")
    assert result == [{"id": "d1", "title": "Inception"}]


@pytest.mark.asyncio
async def test_search_discs_returns_empty_on_network_error():
    client = _mock_client(raise_on_request=httpx.ConnectError("connection refused"))
    with patch("httpx.AsyncClient", return_value=client):
        result = await catalog_client.search_discs(title="Inception")
    assert result == []


# ── get_disc ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_disc_returns_none_on_404():
    client = _mock_client(get_return=_response(status_code=404))
    with patch("httpx.AsyncClient", return_value=client):
        result = await catalog_client.get_disc("missing-id")
    assert result is None


@pytest.mark.asyncio
async def test_get_disc_returns_none_when_unreachable():
    client = _mock_client(raise_on_request=httpx.ConnectError("connection refused"))
    with patch("httpx.AsyncClient", return_value=client):
        result = await catalog_client.get_disc("some-id")
    assert result is None


@pytest.mark.asyncio
async def test_get_disc_returns_data_when_found():
    client = _mock_client(get_return=_response(json_data={"id": "d1", "title": "Inception"}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await catalog_client.get_disc("d1")
    assert result == {"id": "d1", "title": "Inception"}


# ── create_disc ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_disc_returns_created_record():
    client = _mock_client(post_return=_response(json_data={"id": "new-id", "title": "New Disc"}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await catalog_client.create_disc({"title": "New Disc"})
    assert result == {"id": "new-id", "title": "New Disc"}


@pytest.mark.asyncio
async def test_create_disc_raises_on_failure():
    client = _mock_client(post_return=_response(status_code=500))
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(httpx.HTTPStatusError):
            await catalog_client.create_disc({"title": "New Disc"})


# ── link_delivered_files ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_link_delivered_files_returns_true_on_success():
    client = _mock_client(post_return=_response(json_data={"linked": ["/a.mkv"]}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await catalog_client.link_delivered_files(["/a.mkv"], "disc-1")
    assert result is True


@pytest.mark.asyncio
async def test_link_delivered_files_returns_false_when_unreachable():
    client = _mock_client(raise_on_request=httpx.ConnectError("connection refused"))
    with patch("httpx.AsyncClient", return_value=client):
        result = await catalog_client.link_delivered_files(["/a.mkv"], "disc-1")
    assert result is False
