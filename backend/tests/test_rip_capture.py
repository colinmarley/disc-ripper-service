"""
The rip loop and delivery, with makemkvcon replaced by a scripted fake.

Covers what the rip-time capture work is for: every output file is tied to
the disc title it came from, a title that fails does not shift the others,
nothing is overwritten on delivery, and a manifest with the full record is
written next to the files.
"""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import settings
from db.database import SessionLocal
from db.models import RipJob, RipTitle
from services import manifest as manifest_service
from services.job_manager import RipLineParser, _unique_dest_name, job_manager
from services.makemkv_service import parse_scan, save_scan

FIXTURES = Path(__file__).parent / "fixtures" / "makemkv"

CATALOG_DISC = {
    "id": "disc-1", "title": "Firefly Disc 1", "barcode": "024543012345",
    "discNumber": 1, "edition": "Complete Series", "format": "dvd", "setId": "release-9",
}


async def _null_log(_line: str):
    pass


class _FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return (self._lines.pop(0) + "\n").encode()


class _FakeProc:
    def __init__(self, lines, returncode):
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode
        self.terminated = False

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


def fake_makemkv(behaviour: dict[int, dict], procs: list | None = None):
    """Stand-in for asyncio.create_subprocess_exec.

    `behaviour` maps a disc title index to {"files": [...], "returncode": int,
    "lines": [...]}; the listed files are created in the rip dir.
    """
    async def _exec(*args, **_kwargs):
        idx, rip_dir = int(args[-2]), Path(args[-1])
        spec = behaviour.get(idx, {})
        for name in spec.get("files", []):
            (rip_dir / name).write_bytes(f"mkv-data-title-{idx}-{name}".encode())
        proc = _FakeProc(spec.get("lines", []), spec.get("returncode", 0))
        if procs is not None:
            procs.append(proc)
        return proc
    return _exec


async def _rip_and_deliver(job_id: str, behaviour: dict, catalog_disc=CATALOG_DISC):
    with patch("services.job_manager.asyncio.create_subprocess_exec", new=fake_makemkv(behaviour)), \
         patch("services.job_manager.catalog_client.get_disc", new=AsyncMock(return_value=catalog_disc)), \
         patch("services.job_manager.catalog_client.put_rip_record", new=AsyncMock(return_value=(True, None))),          patch("services.job_manager.catalog_client.link_delivered_files", new=AsyncMock(return_value=True)):
        await job_manager._run_rip(job_id, _null_log)
        await job_manager._run_deliver(job_id, _null_log)


def _job(job_id: str) -> RipJob:
    with SessionLocal() as db:
        return db.get(RipJob, job_id)


def _titles(job_id: str) -> list[RipTitle]:
    with SessionLocal() as db:
        return db.query(RipTitle).filter(RipTitle.job_id == job_id).order_by(RipTitle.id).all()


def _show_job(make_job, title: str, **kwargs) -> str:
    return make_job(
        status="queued", media_type="show", title=title, year=2002, season=1,
        mkv_title_indices=[0, 1, 2],
        episode_map={"0": "S01E01 - Serenity", "1": "S01E02 - The Train Job", "2": "S01E03 - Bushwhacked"},
        **kwargs,
    )


# ── Line parser ─────────────────────────────────────────────────────────────

def test_line_parser_counts_read_errors_and_reads_drive_label():
    parser = RipLineParser()
    assert parser.feed('DRV:0,2,999,1,"BD-RE HL-DT-ST","FIREFLY_D1","/dev/sr0"') == []
    assert parser.feed('DRV:1,256,999,0,"","",""') == []
    assert parser.drive_label == "FIREFLY_D1"

    assert parser.feed("PRGV:500,0,1000") == []
    assert parser.fraction == 0.5

    first = parser.feed("MSG:2003,0,3,\"Error 'Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR' occurred while reading '/VIDEO_TS/VTS_01_1.VOB' at offset '1234'\"")
    assert len(first) == 1 and first[0].startswith("[warn] Read error:")
    for _ in range(2):
        assert len(parser.feed("MSG:2003,0,3,\"Error 'Scsi error' occurred while reading '/VIDEO_TS/VTS_01_1.VOB' at offset '99'\"")) == 1
    assert len(parser.feed("MSG:2003,0,3,\"Error 'Scsi error' occurred while reading '/VIDEO_TS/VTS_01_1.VOB' at offset '100'\"")) == 1
    assert parser.feed("MSG:2003,0,3,\"Error 'Scsi error' occurred while reading '/VIDEO_TS/VTS_01_1.VOB' at offset '101'\"") == []
    assert parser.read_errors == 5
    assert parser.source_warnings == 0

    assert parser.feed('MSG:5036,0,1,"Copy complete. 1 titles saved.","Copy complete."') == [
        "[rip] Copy complete. 1 titles saved."
    ]
    assert "Copy complete. 1 titles saved." in parser.messages


def test_structure_warnings_are_not_read_errors():
    """Captured from the drive: a DVD title that rips perfectly emits the same
    31 of these on every pass. They must not flag the title as damaged."""
    warning = (
        "MSG:4004,16777216,2,\"The source file '/VIDEO_TS/VTS_05_1.VOB' is corrupt or invalid at offset 8192, "
        "attempting to work around\",\"The source file '%1' is corrupt or invalid at offset %2, "
        "attempting to work around\",\"/VIDEO_TS/VTS_05_1.VOB\",\"8192\""
    )
    parser = RipLineParser()
    logged = [line for _ in range(31) for line in parser.feed(warning)]

    assert parser.read_errors == 0
    assert parser.source_warnings == 31
    assert logged == ["[warn] /VIDEO_TS/VTS_05_1.VOB has invalid structure — MakeMKV is working around it"]
    assert parser.warning_summary() == [
        "31 structure warning(s) in /VIDEO_TS/VTS_05_1.VOB, worked around by MakeMKV"
    ]


@pytest.mark.asyncio
async def test_structure_warnings_reach_the_manifest_as_a_note_only(make_job):
    job_id = make_job(status="queued", title="Protected Disc", year=2009, mkv_title_indices=[0])
    warning = (
        "MSG:4004,16777216,2,\"The source file '/VIDEO_TS/VTS_05_1.VOB' is corrupt or invalid at offset 8192, "
        "attempting to work around\",\"x\",\"/VIDEO_TS/VTS_05_1.VOB\",\"8192\""
    )
    await _rip_and_deliver(job_id, {0: {"files": ["t00.mkv"], "lines": [warning] * 31}})

    row = _titles(job_id)[0]
    assert row.read_errors == 0
    assert "31 structure warning(s) in /VIDEO_TS/VTS_05_1.VOB, worked around by MakeMKV" in row.messages


# ── Rip loop ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_middle_title_does_not_shift_the_others(make_job):
    """Title 1 yields no file. Titles 0 and 2 must keep their own episode
    names — pairing files to titles by position would mislabel title 2."""
    job_id = _show_job(make_job, "Shiftless Show", catalog_disc_id="disc-1")
    await _rip_and_deliver(job_id, {
        0: {"files": ["B1_t00.mkv"]},
        1: {"files": []},
        2: {"files": ["B3_t02.mkv"]},
    })

    job = _job(job_id)
    assert job.status == "done"
    assert "1 of 3 title(s) failed" in job.error
    names = sorted(Path(p).name for p in job.output_paths)
    assert names == [
        "Shiftless Show S01E01 - Serenity.mkv",
        "Shiftless Show S01E03 - Bushwhacked.mkv",
    ]

    rows = {r.title_index: r for r in _titles(job_id)}
    assert rows[0].status == "delivered" and rows[0].makemkv_filename == "B1_t00.mkv"
    assert rows[1].status == "failed" and rows[1].dest_path is None
    assert rows[2].status == "delivered" and rows[2].makemkv_filename == "B3_t02.mkv"
    assert rows[2].dest_path.endswith("S01E03 - Bushwhacked.mkv")
    # The delivered bytes really are title 2's
    assert b"title-2" in Path(rows[2].dest_path).read_bytes()

    manifest = manifest_service.read_stored_manifest(job_id)
    assert manifest["status"] == "partial"


@pytest.mark.asyncio
async def test_nonzero_exit_discards_partial_file(make_job):
    job_id = _show_job(make_job, "Partial Show")
    await _rip_and_deliver(job_id, {
        0: {"files": ["B1_t00.mkv"]},
        1: {"files": ["B2_t01.mkv"], "returncode": 1},
        2: {"files": ["B3_t02.mkv"]},
    })
    rows = {r.title_index: r for r in _titles(job_id)}
    assert rows[1].status == "failed"
    assert rows[1].error == "makemkvcon exited 1"
    assert len(_job(job_id).output_paths) == 2


@pytest.mark.asyncio
async def test_job_fails_when_no_title_ripped(make_job):
    job_id = make_job(status="queued", title="Nothing Ripped", mkv_title_indices=[0, 1])
    with patch("services.job_manager.asyncio.create_subprocess_exec", new=fake_makemkv({})):
        with pytest.raises(RuntimeError, match="No titles were ripped"):
            await job_manager._run_rip(job_id, _null_log)
    assert [r.status for r in _titles(job_id)] == ["failed", "failed"]


@pytest.mark.asyncio
async def test_title_producing_two_files_records_both(make_job):
    job_id = make_job(status="queued", title="Two File Movie", year=2011, mkv_title_indices=[4])
    await _rip_and_deliver(job_id, {4: {"files": ["title_t04.mkv", "title_t04_1.mkv"]}})

    rows = _titles(job_id)
    assert [(r.title_index, r.file_seq) for r in rows] == [(4, 0), (4, 1)]
    paths = _job(job_id).output_paths
    assert len(paths) == len(set(paths)) == 2
    assert all(Path(p).is_file() for p in paths)


@pytest.mark.asyncio
async def test_rip_records_size_checksum_and_read_errors(make_job):
    job_id = make_job(status="queued", title="Checked Movie", year=2012, mkv_title_indices=[0])
    lines = [
        'DRV:0,2,999,1,"BD-RE HL-DT-ST","SOME_DISC","/dev/sr0"',
        "MSG:2003,0,3,\"Error 'x' occurred while reading '/VIDEO_TS/VTS_01_1.VOB' at offset '1'\"",
        "MSG:2003,0,3,\"Error 'x' occurred while reading '/VIDEO_TS/VTS_01_1.VOB' at offset '2'\"",
        'MSG:5036,0,1,"Copy complete. 1 titles saved.","Copy complete."',
    ]
    await _rip_and_deliver(job_id, {0: {"files": ["title_t00.mkv"], "lines": lines}})

    row = _titles(job_id)[0]
    data = Path(row.dest_path).read_bytes()
    assert row.size_bytes == len(data)
    assert row.sha256 == hashlib.sha256(data).hexdigest()
    assert row.read_errors == 2
    assert row.rip_started_at and row.rip_finished_at
    job = _job(job_id)
    assert job.rip_started_at and job.rip_finished_at and job.delivered_at


@pytest.mark.asyncio
async def test_duration_mismatch_against_scan_is_noted(make_job):
    raw = (FIXTURES / "bluray_movie_info.txt").read_text(encoding="utf-8")
    scan = save_scan(raw, parse_scan(raw))
    job_id = make_job(
        status="queued", title="Short Rip", year=2014, mkv_title_indices=[0],
        scan_id=scan["scan_id"], scan_link="declared", fingerprint=scan["fingerprint"],
    )
    # Scan says 8936 s; the "ripped" file reports half that.
    with patch("services.job_manager.probe_service.probe_file", return_value={"duration_seconds": 4468.0}):
        await _rip_and_deliver(job_id, {0: {"files": ["Gone Girl_t00.mkv"]}})
    assert any("Duration mismatch" in m for m in _titles(job_id)[0].messages)


@pytest.mark.asyncio
async def test_declared_scan_wrong_disc_fails_job(make_job):
    raw = (FIXTURES / "dvd_tv_info.txt").read_text(encoding="utf-8")
    scan = save_scan(raw, parse_scan(raw))
    job_id = make_job(
        status="queued", title="Wrong Disc", mkv_title_indices=[0],
        scan_id=scan["scan_id"], scan_link="declared", fingerprint=scan["fingerprint"],
    )
    procs: list = []
    behaviour = {0: {"files": ["B1_t00.mkv"], "lines": ['DRV:0,2,999,1,"BD-RE","OTHER_MOVIE","/dev/sr0"']}}
    with patch("services.job_manager.asyncio.create_subprocess_exec", new=fake_makemkv(behaviour, procs)):
        with pytest.raises(RuntimeError, match="not the disc this job was configured from"):
            await job_manager._run_rip(job_id, _null_log)
    assert procs[0].terminated is True


@pytest.mark.asyncio
async def test_inferred_scan_wrong_disc_drops_scan_and_continues(make_job):
    raw = (FIXTURES / "dvd_tv_info.txt").read_text(encoding="utf-8")
    scan = save_scan(raw, parse_scan(raw))
    job_id = make_job(
        status="queued", title="Swapped Disc", mkv_title_indices=[0],
        scan_id=scan["scan_id"], scan_link="inferred", fingerprint=scan["fingerprint"],
    )
    behaviour = {0: {"files": ["title_t00.mkv"], "lines": ['DRV:0,2,999,1,"BD-RE","OTHER_MOVIE","/dev/sr0"']}}
    await _rip_and_deliver(job_id, behaviour)

    job = _job(job_id)
    assert job.status == "done"
    assert job.scan_id is None and job.fingerprint is None
    assert manifest_service.read_stored_manifest(job_id)["disc"]["volume_label"] is None


@pytest.mark.asyncio
async def test_matching_disc_label_keeps_scan(make_job):
    raw = (FIXTURES / "dvd_tv_info.txt").read_text(encoding="utf-8")
    scan = save_scan(raw, parse_scan(raw))
    job_id = make_job(
        status="queued", title="Right Disc", mkv_title_indices=[0],
        scan_id=scan["scan_id"], scan_link="declared", fingerprint=scan["fingerprint"],
    )
    behaviour = {0: {"files": ["B1_t00.mkv"], "lines": ['DRV:0,2,999,1,"BD-RE","firefly_d1","/dev/sr0"']}}
    await _rip_and_deliver(job_id, behaviour)
    assert _job(job_id).scan_id == scan["scan_id"]


@pytest.mark.asyncio
async def test_cancel_during_rip_stays_cancelled(make_job):
    job_id = make_job(status="queued", title="Cancelled Movie", mkv_title_indices=[0])

    async def _exec(*args, **_kwargs):
        job_manager._update_field(job_id, status="cancelled")
        return _FakeProc([], returncode=-15)

    with patch("services.job_manager.asyncio.create_subprocess_exec", new=_exec):
        with pytest.raises(asyncio.CancelledError):
            await job_manager._run_rip(job_id, _null_log)


# ── Delivery naming ─────────────────────────────────────────────────────────

def test_unique_dest_name(tmp_path):
    assert _unique_dest_name(tmp_path, "Movie (2020).mkv", set()) == "Movie (2020).mkv"
    (tmp_path / "Movie (2020).mkv").write_bytes(b"x")
    assert _unique_dest_name(tmp_path, "Movie (2020).mkv", set()) == "Movie (2020) - Copy 2.mkv"
    # extras suffix stays at the very end of the stem
    assert _unique_dest_name(
        tmp_path, "Movie (2020)-trailer.mkv", {"movie (2020)-trailer.mkv"}
    ) == "Movie (2020) - Copy 2-trailer.mkv"


@pytest.mark.asyncio
async def test_delivery_never_overwrites_an_existing_file(make_job):
    """A second disc (or a re-rip) of the same film lands in the same folder
    under the same computed name; the first file must survive."""
    first = make_job(status="queued", title="Twice Ripped", year=2019, mkv_title_indices=[0])
    await _rip_and_deliver(first, {0: {"files": ["a.mkv"]}})
    second = make_job(status="queued", title="Twice Ripped", year=2019, mkv_title_indices=[1])
    await _rip_and_deliver(second, {1: {"files": ["b.mkv"]}})

    one, two = _job(first).output_paths[0], _job(second).output_paths[0]
    assert Path(one).name == "Twice Ripped (2019).mkv"
    assert Path(two).name == "Twice Ripped (2019) - Copy 2.mkv"
    assert b"title-0" in Path(one).read_bytes()
    assert b"title-1" in Path(two).read_bytes()


@pytest.mark.asyncio
async def test_movie_with_extra_keeps_plain_main_feature_name(make_job):
    """One feature plus a trailer is not 'Version 1': extras do not count
    towards the multi-version naming."""
    job_id = make_job(
        status="queued", title="Feature And Trailer", year=2018,
        mkv_title_indices=[0, 3], title_content_types={"3": "trailer"},
    )
    await _rip_and_deliver(job_id, {0: {"files": ["t00.mkv"]}, 3: {"files": ["t03.mkv"]}})
    assert sorted(Path(p).name for p in _job(job_id).output_paths) == [
        "Feature And Trailer (2018)-trailer.mkv",
        "Feature And Trailer (2018).mkv",
    ]


# ── Manifest ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manifest_written_with_full_record(make_job):
    raw = (FIXTURES / "dvd_tv_info.txt").read_text(encoding="utf-8")
    scan = save_scan(raw, parse_scan(raw))
    job_id = make_job(
        status="queued", media_type="show", title="Manifest Show", year=2002, season=1,
        imdb_id="tt0303461", tmdb_id=1437, catalog_disc_id="disc-1",
        mkv_title_indices=[0, 1, 2],
        scan_id=scan["scan_id"], scan_link="declared", fingerprint=scan["fingerprint"],
        title_declarations=[
            {"title_index": 0, "season": 1, "episode": 1, "episode_end": 2,
             "tmdb_episode_id": 85395, "episode_name": "Serenity"},
            {"title_index": 1, "season": 1, "episode": 3, "tmdb_episode_id": 85396,
             "episode_name": "The Train Job"},
            {"title_index": 2, "content_type": "trailer"},
        ],
        episode_map={"0": "S01E01-E02 - Serenity", "1": "S01E03 - The Train Job"},
        title_content_types={"2": "trailer"},
    )
    await _rip_and_deliver(job_id, {
        0: {"files": ["B1_t00.mkv"]}, 1: {"files": ["B2_t01.mkv"]}, 2: {"files": ["B9_t02.mkv"]},
    })

    title_dir = Path(settings.ingest_root) / "Manifest Show (2002) [imdbid-tt0303461]"
    in_folder = title_dir / f"_rip_manifest.{job_id[:8]}.json"
    assert in_folder.is_file(), "manifest sits in the title folder, not the Season subfolder"
    manifest = json.loads(in_folder.read_text(encoding="utf-8"))
    assert manifest == manifest_service.read_stored_manifest(job_id)
    manifest_service.Manifest.model_validate(manifest)

    assert manifest["schema_version"] == 1
    assert manifest["status"] == "delivered"
    assert manifest["job_id"] == job_id
    assert manifest["catalog"] == {
        "disc_id": "disc-1", "title": "Firefly Disc 1", "barcode": "024543012345",
        "disc_number": 1, "edition": "Complete Series", "format": "dvd", "release_id": "release-9",
    }
    assert manifest["identity"] == {
        "media_type": "show", "title": "Manifest Show", "year": 2002,
        "imdb_id": "tt0303461", "tmdb_id": 1437, "season": 1,
    }
    disc = manifest["disc"]
    assert disc["volume_label"] == "FIREFLY_D1"
    assert disc["reported_disc_type"] == "DVD disc"
    assert disc["fingerprint"] == scan["fingerprint"]
    assert disc["fingerprint_algo"] == "scan-v1"
    assert disc["scan_link"] == "declared"
    assert disc["makemkv_version"].startswith("MakeMKV v1.17.7")
    assert disc["skipped_short"] == 1

    # Every title on the disc is recorded, with its streams
    assert [t["index"] for t in manifest["scan_titles"]] == [0, 1, 2]
    assert manifest["scan_titles"][0]["streams"][1]["lang_code"] == "eng"

    by_index = {t["title_index"]: t for t in manifest["titles"]}
    double = by_index[0]
    assert (double["season"], double["episode"], double["episode_end"]) == (1, 1, 2)
    assert double["tmdb_episode_id"] == 85395
    assert double["content_type"] == "episode"
    assert double["makemkv_filename"] == "B1_t00.mkv"
    assert double["status"] == "delivered"
    assert double["ingest_relative_path"] == (
        "Manifest Show (2002) [imdbid-tt0303461]/Season 01/Manifest Show S01E01-E02 - Serenity.mkv"
    )
    assert Path(double["dest_path"]).is_file()
    assert double["sha256"] and double["size_bytes"]
    assert by_index[2]["content_type"] == "trailer"
    assert by_index[2]["episode"] is None


@pytest.mark.asyncio
async def test_two_discs_into_one_folder_keep_two_manifests(make_job):
    common = dict(status="queued", media_type="show", title="Two Disc Show", year=2005, season=1)
    disc1 = make_job(mkv_title_indices=[0], episode_map={"0": "S01E01"}, **common)
    await _rip_and_deliver(disc1, {0: {"files": ["a.mkv"]}})
    disc2 = make_job(mkv_title_indices=[0], episode_map={"0": "S01E05"}, **common)
    await _rip_and_deliver(disc2, {0: {"files": ["b.mkv"]}})

    title_dir = Path(settings.ingest_root) / "Two Disc Show (2005)"
    manifests = sorted(p.name for p in title_dir.glob("_rip_manifest.*.json"))
    assert manifests == sorted([f"_rip_manifest.{disc1[:8]}.json", f"_rip_manifest.{disc2[:8]}.json"])


@pytest.mark.asyncio
async def test_manifest_exists_before_any_file_is_moved(make_job):
    job_id = make_job(status="queued", title="Ordered Delivery", year=2001, mkv_title_indices=[0])
    title_dir = Path(settings.ingest_root) / "Ordered Delivery (2001)"
    seen: dict = {}
    real_move = __import__("shutil").move

    def _checking_move(src, dst):
        on_disk = json.loads((title_dir / f"_rip_manifest.{job_id[:8]}.json").read_text(encoding="utf-8"))
        seen["status"] = on_disk["status"]
        seen["dest_path"] = on_disk["titles"][0]["dest_path"]
        return real_move(src, dst)

    with patch("services.job_manager.shutil.move", new=_checking_move):
        await _rip_and_deliver(job_id, {0: {"files": ["a.mkv"]}})

    assert seen["status"] == "delivering"
    assert seen["dest_path"] == _job(job_id).output_paths[0]


@pytest.mark.asyncio
async def test_failed_job_still_keeps_a_manifest(make_job):
    job_id = make_job(status="queued", title="Failed Record", mkv_title_indices=[0])
    with patch("services.job_manager.asyncio.create_subprocess_exec", new=fake_makemkv({})):
        await job_manager._process_job(job_id)
    assert _job(job_id).status == "failed"
    manifest = manifest_service.read_stored_manifest(job_id)
    assert manifest["status"] == "failed"
    assert manifest["titles"][0]["status"] == "failed"


# ── Declaration helpers ─────────────────────────────────────────────────────

def test_parse_episode_value():
    assert manifest_service.parse_episode_value("S01E03 - Pilot") == {
        "season": 1, "episode": 3, "episode_end": None, "episode_name": "Pilot",
    }
    assert manifest_service.parse_episode_value("S02E05-E06")["episode_end"] == 6
    assert manifest_service.parse_episode_value("s00e01")["season"] == 0
    assert manifest_service.parse_episode_value("Making Of") == {"custom_name": "Making Of"}


def test_episode_code_round_trip():
    assert manifest_service.episode_code(
        {"season": 1, "episode": 1, "episode_end": 2, "episode_name": "Serenity"}
    ) == "S01E01-E02 - Serenity"
    assert manifest_service.episode_code({"season": 3, "episode": 12}) == "S03E12"
    assert manifest_service.episode_code({"custom_name": "Gag Reel"}) == "Gag Reel"
    assert manifest_service.episode_code({"content_type": "trailer"}) is None


def test_derive_legacy_maps():
    episode_map, content_types = manifest_service.derive_legacy_maps([
        {"title_index": 0, "season": 1, "episode": 1, "episode_name": "Pilot"},
        {"title_index": 1, "content_type": "deleted_scene"},
        {"title_index": 2, "content_type": "main_feature"},
    ])
    assert episode_map == {"0": "S01E01 - Pilot"}
    assert content_types == {"1": "deleted_scene"}


def test_declarations_fall_back_to_legacy_fields(make_job):
    movie = make_job(
        status="queued", title="Legacy Movie", mkv_title_indices=[0, 2],
        title_content_types={"2": "featurette"},
    )
    decl = manifest_service.declarations_for(_job(movie))
    assert decl[0]["content_type"] == "main_feature" and decl[0]["is_main_feature"] is True
    assert decl[2]["content_type"] == "featurette" and not decl[2]["is_main_feature"]

    show = make_job(
        status="queued", media_type="show", title="Legacy Show", season=2,
        mkv_title_indices=[0, 1], episode_map={"0": "S02E04 - Name"},
    )
    decl = manifest_service.declarations_for(_job(show))
    assert (decl[0]["season"], decl[0]["episode"], decl[0]["episode_name"]) == (2, 4, "Name")
    assert decl[1]["content_type"] == "episode" and decl[1]["season"] == 2
    assert decl[1].get("episode") is None


# ── Wrong-disc check from the disc structure ────────────────────────────────
# With --noscan the drive line carries no disc label on the real host, so the
# check that actually fires there is the "Title #N was added" comparison.

REAL_SCAN = FIXTURES / "dvd_tv_real_info.txt"


def _real_scan_job(make_job, title: str, link: str = "declared") -> tuple[str, list[str]]:
    raw = REAL_SCAN.read_text(encoding="utf-8")
    scan = save_scan(raw, parse_scan(raw))
    added = [line for line in raw.splitlines() if line.startswith("MSG:3028,")]
    job_id = make_job(
        status="queued", media_type="show", title=title, year=2007, season=1,
        mkv_title_indices=[2], episode_map={"2": "S01E05"},
        scan_id=scan["scan_id"], scan_link=link, fingerprint=scan["fingerprint"],
    )
    return job_id, added


@pytest.mark.asyncio
async def test_same_disc_structure_passes_the_check(make_job):
    job_id, added = _real_scan_job(make_job, "Same Structure")
    await _rip_and_deliver(job_id, {2: {"files": ["C1_t02.mkv"], "lines": added}})
    job = _job(job_id)
    assert job.status == "done" and job.scan_id is not None


@pytest.mark.asyncio
async def test_different_disc_structure_fails_a_declared_scan(make_job):
    job_id, added = _real_scan_job(make_job, "Other Structure")
    foreign = 'MSG:3028,0,3,"Title #1 was added (31 cell(s), 2:01:44)","Title #%1 was added (%2 cell(s), %3)","1","31","2:01:44"'
    procs: list = []
    behaviour = {2: {"files": ["C1_t02.mkv"], "lines": [foreign] + added}}
    with patch("services.job_manager.asyncio.create_subprocess_exec", new=fake_makemkv(behaviour, procs)):
        with pytest.raises(RuntimeError, match="not the disc this job was configured from"):
            await job_manager._run_rip(job_id, _null_log)
    assert procs[0].terminated is True


@pytest.mark.asyncio
async def test_disc_check_can_be_switched_off(make_job, monkeypatch):
    monkeypatch.setattr(settings, "verify_disc_matches_scan", False)
    job_id, _ = _real_scan_job(make_job, "Unchecked Structure")
    foreign = 'MSG:3028,0,3,"Title #1 was added (31 cell(s), 2:01:44)","Title #%1 was added (%2 cell(s), %3)","1","31","2:01:44"'
    await _rip_and_deliver(job_id, {2: {"files": ["C1_t02.mkv"], "lines": [foreign]}})
    assert _job(job_id).status == "done"
