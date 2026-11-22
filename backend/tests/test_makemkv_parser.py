"""Scan parsing: everything makemkvcon reports is kept, and the disc
fingerprint is stable."""

import copy
from pathlib import Path

from services.makemkv_service import (
    _parse_info_output,
    fingerprint,
    parse_drv_line,
    parse_scan,
    save_scan,
    split_robot_fields,
)

FIXTURES = Path(__file__).parent / "fixtures" / "makemkv"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_split_fields_handles_commas_and_escaped_quotes():
    assert split_robot_fields('0,2,2,5092,"Stereo, commentary \\"cast\\""') == [
        "0", "2", "2", "5092", 'Stereo, commentary "cast"',
    ]
    assert split_robot_fields('1,6206,"DVD disc"') == ["1", "6206", "DVD disc"]
    assert split_robot_fields('0,27,0,""') == ["0", "27", "0", ""]


def test_parse_drv_line_skips_blank_entries():
    assert parse_drv_line('DRV:1,256,999,0,"","",""') is None
    drv = parse_drv_line('DRV:0,2,999,1,"BD-RE HL-DT-ST","FIREFLY_D1","/dev/sr0"')
    assert drv["model"] == "BD-RE HL-DT-ST"
    assert drv["label"] == "FIREFLY_D1"
    assert drv["device"] == "/dev/sr0"


def test_dvd_scan_disc_identity():
    scan = parse_scan(_load("dvd_tv_info.txt"))
    assert scan["makemkv_version"] == "MakeMKV v1.17.7 linux(x64-release)"
    assert scan["disc"]["disc_type"] == "DVD disc"
    assert scan["disc"]["name"] == "Firefly, Disc 1"
    assert scan["disc"]["volume_label"] == "FIREFLY_D1"
    assert scan["disc"]["lang_code"] == "eng"
    assert scan["drive"]["label"] == "FIREFLY_D1"
    assert scan["drive"]["device"] == "/dev/sr0"
    assert scan["skipped_short"] == 1
    assert scan["skipped_other"] == 1
    # Unnamed attributes survive in the raw map
    assert scan["disc"]["attrs"]["33"] == "0"


def test_dvd_scan_titles_and_streams():
    scan = parse_scan(_load("dvd_tv_info.txt"))
    assert [t["index"] for t in scan["titles"]] == [0, 1, 2]

    t0 = scan["titles"][0]
    assert t0["duration_seconds"] == 5171
    assert t0["chapter_count"] == 12
    assert t0["file_size_bytes"] == 4959793152
    assert t0["output_filename"] == "B1_t00.mkv"
    assert t0["segment_count"] == 1
    assert t0["segment_map"] == "1-12"
    assert t0["original_title_id"] == "01"
    assert t0["name"] == "12 chapter(s) , 4.6 GB (B1)"   # description line, as before
    assert t0["title_name"] == ""                          # attr 2 is absent on this DVD
    assert (t0["width"], t0["height"], t0["codec"]) == (720, 480, "Mpeg2")
    assert t0["attrs"]["10"] == "4.6 GB"

    video, surround, commentary, subs = t0["streams"]
    assert video["type"] == "video"
    assert video["aspect"] == "16:9"
    assert video["frame_rate"].startswith("29.97")
    assert surround["type"] == "audio"
    assert surround["lang_code"] == "eng"
    assert surround["channels"] == 6
    assert surround["channel_layout"] == "5.1(side)"
    assert surround["sample_rate"] == 48000
    assert surround["default"] is True
    assert commentary["name"] == 'Stereo, commentary "cast"'
    assert commentary["default"] is False
    assert subs["type"] == "subtitles"
    assert subs["lang_name"] == "Spanish"
    assert subs["forced"] is True


def test_bluray_scan_keeps_playlist_and_title_name():
    scan = parse_scan(_load("bluray_movie_info.txt"))
    assert scan["disc"]["disc_type"] == "Blu-ray disc"
    main, extra = scan["titles"]
    assert main["title_name"] == "Gone Girl"
    assert main["source_file"] == "00800.mpls"
    assert extra["source_file"] == "00021.mpls"
    assert (main["width"], main["height"]) == (1920, 1080)
    forced = [s for s in main["streams"] if s["forced"]]
    assert len(forced) == 1 and forced[0]["type"] == "subtitles"


def test_legacy_wrapper_returns_title_list():
    titles = _parse_info_output(_load("dvd_tv_info.txt"))
    assert len(titles) == 3
    for key in ("index", "name", "duration_seconds", "chapter_count",
                "file_size_bytes", "output_filename", "streams", "width", "height", "codec"):
        assert key in titles[0]


def test_no_disc_output_has_no_titles_and_no_fingerprint():
    scan = parse_scan(
        'MSG:1005,0,1,"MakeMKV v1.17.7 started","%1 started","MakeMKV v1.17.7"\n'
        'DRV:0,0,999,0,"BD-RE HL-DT-ST","","/dev/sr0"\n'
        'MSG:5010,0,0,"Failed to open disc","Failed to open disc"\n'
        "TCOUNT:0\n"
    )
    assert scan["titles"] == []
    assert scan["drive"]["model"] == "BD-RE HL-DT-ST"
    assert fingerprint(scan) is None


def test_fingerprint_is_stable_and_order_independent():
    scan = parse_scan(_load("dvd_tv_info.txt"))
    fp = fingerprint(scan)
    assert fp and len(fp) == 64
    assert fingerprint(parse_scan(_load("dvd_tv_info.txt"))) == fp

    shuffled = copy.deepcopy(scan)
    shuffled["titles"].reverse()
    for new_index, title in enumerate(shuffled["titles"]):
        title["index"] = new_index
    assert fingerprint(shuffled) == fp


def test_fingerprint_ignores_short_titles_and_version():
    scan = parse_scan(_load("dvd_tv_info.txt"))
    fp = fingerprint(scan)

    without_short = copy.deepcopy(scan)
    without_short["titles"] = [t for t in without_short["titles"] if t["duration_seconds"] >= 120]
    assert len(without_short["titles"]) == 2
    assert fingerprint(without_short) == fp

    other_version = copy.deepcopy(scan)
    other_version["makemkv_version"] = "MakeMKV v1.18.0"
    assert fingerprint(other_version) == fp


def test_fingerprint_differs_between_discs():
    assert fingerprint(parse_scan(_load("dvd_tv_info.txt"))) != fingerprint(
        parse_scan(_load("bluray_movie_info.txt"))
    )


def test_save_scan_persists_raw_and_parsed():
    from db.database import SessionLocal
    from db.models import DiscScan

    raw = _load("bluray_movie_info.txt")
    summary = save_scan(raw, parse_scan(raw))
    assert summary["volume_label"] == "GONE_GIRL"
    assert summary["drive_label"] == "GONE_GIRL"
    assert summary["title_count"] == 2
    assert summary["fingerprint_algo"] == "scan-v1"

    with SessionLocal() as db:
        row = db.get(DiscScan, summary["scan_id"])
        assert row.raw_output == raw
        assert row.parsed["titles"][0]["source_file"] == "00800.mpls"
        assert row.fingerprint == summary["fingerprint"]


def test_real_dvd_scan():
    """Captured from the drive (MakeMKV 2.0.0, a 2-disc TV season DVD)."""
    scan = parse_scan(_load("dvd_tv_real_info.txt"))
    assert scan["makemkv_version"] == "MakeMKV v2.0.0 linux(x64-release)"
    assert scan["disc"]["disc_type"] == "DVD disc"
    assert scan["disc"]["volume_label"] == "BURNNOTICE_S1_D2"
    # With --noscan the drive line has no disc label; the volume name is the label.
    assert scan["drive"]["label"] == ""
    assert scan["drive"]["device"] == "/dev/sr0"
    assert len(scan["titles"]) == 16
    assert scan["skipped_short"] == 28

    episode = scan["titles"][2]
    assert episode["duration_seconds"] == 2542
    assert episode["chapter_count"] == 13
    assert episode["output_filename"] == "C1_t02.mkv"
    assert episode["original_title_id"] == "02"
    assert episode["segment_map"] == "1-10,11-14,15-22,23"
    audio = [s for s in episode["streams"] if s["type"] == "audio"]
    assert [(s["lang_code"], s["channels"], s["default"]) for s in audio] == [("eng", 6, True), ("eng", 2, False)]
    subs = [s for s in episode["streams"] if s["type"] == "subtitles"]
    assert [s["lang_code"] for s in subs] == ["eng", "spa", "fre", "eng"]
    assert len(fingerprint(scan)) == 64
    assert sum(1 for m in scan["messages"] if m["code"] == 3028) >= 16
