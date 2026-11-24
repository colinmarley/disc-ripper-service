"""Letterboxed DVD titles: bars burned into a 4:3 frame are found and cropped away."""

from services.letterbox_service import Crop, choose_crop, encode_command, is_interlaced, most_common_crop

# Five samples of The Whitest Kids U' Know S2 E10: the bars are at lines 58 and 422
# everywhere; one dark scene makes the detector pull the sides in.
WKUK = [Crop(708, 364, 8, 58), Crop(710, 364, 6, 58), Crop(710, 364, 6, 58),
        Crop(706, 364, 8, 58), Crop(674, 364, 40, 58)]


def test_reads_the_crop_cropdetect_reported_most():
    out = "\n".join(
        ["[Parsed_cropdetect_0 @ 0x1] x1:6 x2:715 y1:58 y2:421 w:710 h:364 x:6 y:58 crop=710:364:6:58"] * 5
        + ["[Parsed_cropdetect_0 @ 0x1] crop=694:364:20:58"]
    )
    assert most_common_crop(out) == Crop(710, 364, 6, 58)
    assert most_common_crop("no crop lines") is None


def test_a_letterboxed_four_by_three_title_is_cropped_to_the_box_holding_every_sample():
    assert choose_crop(WKUK, 720, 480, "4:3") == Crop(710, 364, 6, 58)


def test_titles_without_burned_in_bars_are_left_alone():
    full = [Crop(720, 480, 0, 0)] * 5
    assert choose_crop(full, 720, 480, "4:3") is None                    # a 4:3 show
    assert choose_crop(WKUK, 720, 480, "853:480") is None                # an anamorphic 16:9 title
    assert choose_crop(WKUK, 1920, 1080, "16:9") is None                 # HD
    thin = [Crop(720, 464, 0, 8)] * 5
    assert choose_crop(thin, 720, 480, "4:3") is None                    # a few dark lines, not bars


def test_samples_must_agree_on_the_bars():
    dark_scene = WKUK[:4] + [Crop(704, 300, 8, 90)]
    assert choose_crop(dark_scene, 720, 480, "4:3") is None
    assert choose_crop(WKUK[:4] + [None], 720, 480, "4:3") is None
    assert choose_crop([], 720, 480, "4:3") is None


def test_the_crop_is_even_and_grows_rather_than_cuts():
    odd = [Crop(709, 363, 7, 59)] * 5
    assert choose_crop(odd, 720, 480, "4:3") == Crop(710, 364, 6, 58)


def test_encode_copies_audio_subtitles_and_chapters_and_deinterlaces_only_when_needed():
    crop = Crop(710, 364, 6, 58)
    cmd = encode_command("in.mkv", "out.mkv", crop, interlaced=True)
    assert cmd[cmd.index("-vf") + 1] == "bwdif=mode=send_frame:parity=auto:deint=interlaced,crop=710:364:6:58"
    assert ["-map", "0"] == cmd[cmd.index("-map"):cmd.index("-map") + 2]
    assert "-map_chapters" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    progressive = encode_command("in.mkv", "out.mkv", crop, interlaced=False)
    assert progressive[progressive.index("-vf") + 1] == "crop=710:364:6:58"


def test_field_order_says_whether_a_title_is_interlaced():
    assert is_interlaced({"streams": [{"codec_type": "video", "field_order": "tt"}]})
    assert not is_interlaced({"streams": [{"codec_type": "video", "field_order": "progressive"}]})
    assert not is_interlaced(None)
