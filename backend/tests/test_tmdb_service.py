"""Tests for services/tmdb_service.py — mocked httpx, no real network calls."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from config.settings import settings
from services import tmdb_service


def _response(json_data=None):
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    return resp


def _mock_client(get_return):
    client = AsyncMock()
    client.get.return_value = get_return
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


@pytest.fixture(autouse=True)
def with_api_key():
    original = settings.tmdb_api_key
    settings.tmdb_api_key = "test-key"
    yield
    settings.tmdb_api_key = original


@pytest.mark.asyncio
async def test_search_raises_when_not_configured():
    settings.tmdb_api_key = ""
    with pytest.raises(tmdb_service.TmdbNotConfigured):
        await tmdb_service.search("Inception", "movie")


@pytest.mark.asyncio
async def test_search_movie_hits_movie_endpoint():
    client = _mock_client(_response({"results": [{"id": 27205, "title": "Inception"}]}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await tmdb_service.search("Inception", "movie")
    assert result == [{"id": 27205, "title": "Inception"}]
    called_url = client.get.call_args[0][0]
    assert "search/movie" in called_url


@pytest.mark.asyncio
async def test_search_show_hits_tv_endpoint():
    client = _mock_client(_response({"results": [{"id": 1396, "name": "Breaking Bad"}]}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await tmdb_service.search("Breaking Bad", "show")
    assert result == [{"id": 1396, "name": "Breaking Bad"}]
    called_url = client.get.call_args[0][0]
    assert "search/tv" in called_url


@pytest.mark.asyncio
async def test_search_missing_results_key_returns_empty_list():
    client = _mock_client(_response({}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await tmdb_service.search("nonexistent", "movie")
    assert result == []


@pytest.mark.asyncio
async def test_get_season_hits_correct_endpoint():
    client = _mock_client(_response({"season_number": 1, "episodes": []}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await tmdb_service.get_season(1396, 1)
    assert result == {"season_number": 1, "episodes": []}
    called_url = client.get.call_args[0][0]
    assert "/tv/1396/season/1" in called_url


@pytest.mark.asyncio
async def test_get_details_movie_hits_movie_endpoint():
    client = _mock_client(_response({"id": 27205, "title": "Inception"}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await tmdb_service.get_details("movie", 27205)
    assert result["title"] == "Inception"
    called_url = client.get.call_args[0][0]
    assert "/movie/27205" in called_url


@pytest.mark.asyncio
async def test_get_details_show_hits_tv_endpoint():
    client = _mock_client(_response({"id": 1396, "name": "Breaking Bad"}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await tmdb_service.get_details("show", 1396)
    assert result["name"] == "Breaking Bad"
    called_url = client.get.call_args[0][0]
    assert "/tv/1396" in called_url


@pytest.mark.asyncio
async def test_get_details_raises_when_not_configured():
    settings.tmdb_api_key = ""
    with pytest.raises(tmdb_service.TmdbNotConfigured):
        await tmdb_service.get_details("movie", 27205)
