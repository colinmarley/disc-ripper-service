"""Tests for filename sanitization and destination name building."""
import pytest
from services.job_manager import (
    _safe_name,
    _safe_title,
    _episode_filename,
    _ingest_folder_name,
    _build_dest_name,
)
from db.models import RipJob


# ── _safe_name ────────────────────────────────────────────────────────────────

def test_safe_name_spaces_become_underscores():
    assert _safe_name("The Wire") == "The_Wire"


def test_safe_name_strips_exclamation():
    assert "!" not in _safe_name("Hello! World")


def test_safe_name_strips_colon():
    assert ":" not in _safe_name("Mission: Impossible")


def test_safe_name_preserves_dots_dashes():
    assert _safe_name("file-name.v2") == "file-name.v2"


def test_safe_name_empty():
    assert _safe_name("") == ""


# ── _safe_title ───────────────────────────────────────────────────────────────

def test_safe_title_removes_forward_slash():
    assert "/" not in _safe_title("AC/DC: Live")


def test_safe_title_removes_backslash():
    assert "\\" not in _safe_title("path\\injection")


def test_safe_title_removes_null_byte():
    assert _safe_title("Hello\x00World") == "HelloWorld"


def test_safe_title_removes_angle_brackets():
    result = _safe_title("<script>")
    assert "<" not in result
    assert ">" not in result


def test_safe_title_keeps_spaces():
    assert _safe_title("The Dark Knight") == "The Dark Knight"


# ── _episode_filename ─────────────────────────────────────────────────────────

def test_episode_filename_code_only():
    assert _episode_filename("S01E03", "Burn Notice") == "Burn Notice S01E03.mkv"


def test_episode_filename_with_name():
    assert _episode_filename("S01E03 - Pilot", "Burn Notice") == "Burn Notice S01E03 - Pilot.mkv"


def test_episode_filename_special_feature():
    assert _episode_filename("SpecialFeature001", "The Wire") == "The Wire SpecialFeature001.mkv"


# ── _ingest_folder_name ───────────────────────────────────────────────────────

def test_ingest_folder_no_imdb():
    job = RipJob(title="Inception", year=2010, imdb_id=None)
    assert _ingest_folder_name(job) == "Inception (2010)"


def test_ingest_folder_with_imdb():
    job = RipJob(title="Inception", year=2010, imdb_id="tt1375666")
    assert _ingest_folder_name(job) == "Inception (2010) [imdbid-tt1375666]"


# ── _build_dest_name: movie ───────────────────────────────────────────────────

def test_movie_single_title():
    assert _build_dest_name("movie", "Inception", 2010, 0, 1, "0", {}, 1) == "Inception (2010).mkv"


def test_movie_multiple_versions_first():
    assert _build_dest_name("movie", "Inception", 2010, 0, 3, "0", {}, 1) == "Inception (2010) - Version 1.mkv"


def test_movie_multiple_versions_third():
    assert _build_dest_name("movie", "Inception", 2010, 2, 3, "2", {}, 1) == "Inception (2010) - Version 3.mkv"


def test_movie_two_versions_both():
    v1 = _build_dest_name("movie", "Dune", 2021, 0, 2, "0", {}, 1)
    v2 = _build_dest_name("movie", "Dune", 2021, 1, 2, "1", {}, 1)
    assert v1 == "Dune (2021) - Version 1.mkv"
    assert v2 == "Dune (2021) - Version 2.mkv"


# ── _build_dest_name: show ────────────────────────────────────────────────────

def test_show_episode_map_code_only():
    assert _build_dest_name("show", "The Wire", 2002, 0, 1, "0", {"0": "S01E01"}, 1) == "The Wire S01E01.mkv"


def test_show_episode_map_code_and_name():
    ep_map = {"3": "S01E03 - The Target"}
    assert _build_dest_name("show", "The Wire", 2002, 0, 1, "3", ep_map, 1) == "The Wire S01E03 - The Target.mkv"


def test_show_sequential_fallback_episode_1():
    assert _build_dest_name("show", "Burn Notice", 2007, 0, 1, "0", {}, 1) == "Burn Notice S01E01.mkv"


def test_show_sequential_fallback_episode_2():
    assert _build_dest_name("show", "Burn Notice", 2007, 1, 3, "1", {}, 1) == "Burn Notice S01E02.mkv"


def test_show_sequential_fallback_season_2():
    assert _build_dest_name("show", "Burn Notice", 2007, 0, 1, "0", {}, 2) == "Burn Notice S02E01.mkv"


def test_show_partial_map_uses_fallback_for_unmapped():
    ep_map = {"0": "S01E01 - Pilot"}
    # idx "1" not in map → sequential fallback (i=1 → E02)
    result = _build_dest_name("show", "The Wire", 2002, 1, 2, "1", ep_map, 1)
    assert result == "The Wire S01E02.mkv"


def test_show_special_feature_via_map():
    ep_map = {"5": "SpecialFeature001"}
    assert _build_dest_name("show", "The Wire", 2002, 0, 1, "5", ep_map, 1) == "The Wire SpecialFeature001.mkv"


# ── _build_dest_name: content_type (extras taxonomy suffix) ────────────────────

def test_content_type_trailer_gets_suffix_filename():
    result = _build_dest_name(
        "movie", "Inception", 2010, 1, 2, "1", {}, 1, content_type="trailer",
    )
    assert result == "Inception (2010)-trailer.mkv"


def test_content_type_overrides_movie_version_naming():
    # Even though total > 1 (which would normally trigger "- Version N" naming),
    # a set content_type takes priority so the file auto-classifies on ingest.
    result = _build_dest_name(
        "movie", "Dune", 2021, 2, 3, "2", {}, 1, content_type="deleted_scene",
    )
    assert result == "Dune (2021)-deletedscene.mkv"


def test_content_type_unknown_category_falls_back_to_default_naming():
    result = _build_dest_name(
        "movie", "Inception", 2010, 0, 1, "0", {}, 1, content_type="not_a_real_category",
    )
    assert result == "Inception (2010).mkv"


def test_content_type_numbers_duplicate_categories():
    counts: dict[str, int] = {}
    first = _build_dest_name(
        "movie", "Inception", 2010, 0, 3, "0", {}, 1,
        content_type="featurette", content_type_counts=counts,
    )
    second = _build_dest_name(
        "movie", "Inception", 2010, 1, 3, "1", {}, 1,
        content_type="featurette", content_type_counts=counts,
    )
    assert first == "Inception (2010)-featurette.mkv"
    assert second == "Inception (2010) 2-featurette.mkv"


def test_content_type_show_still_uses_suffix_not_episode_naming():
    result = _build_dest_name(
        "show", "The Wire", 2002, 0, 1, "0", {"0": "S01E01"}, 1, content_type="blooper",
    )
    assert result == "The Wire (2002)-blooper.mkv"
