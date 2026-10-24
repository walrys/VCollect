import asyncio

import pytest

from app.pipeline import media
from app.pipeline.errors import PostFailed


def run(coroutine):
    return asyncio.run(coroutine)


def streams(path):
    data = run(media.probe(path))
    return {s["codec_type"]: s for s in data["streams"]}


def test_large_video_is_scaled_to_720_and_reencoded(samples, tmp_path):
    out = tmp_path / "out.mp4"
    info = run(media.compress_video(samples["portrait.mp4"], out))

    assert (info.width, info.height) == (720, 1280)
    found = streams(out)
    assert found["video"]["codec_name"] == "h264"
    assert found["audio"]["codec_name"] == "aac"
    # faststart: the index (moov) comes before the media data (mdat).
    head = out.read_bytes()[:4096]
    assert head.find(b"moov") < head.find(b"mdat") or b"mdat" not in head


def test_landscape_video_keeps_its_shape(samples, tmp_path):
    info = run(media.compress_video(samples["landscape.webm"], tmp_path / "out.mp4"))
    assert (info.width, info.height) == (1280, 720)


def test_light_h264_video_is_kept_as_is(samples, tmp_path):
    src = samples["small.mp4"]
    out = tmp_path / "out.mp4"
    info = run(media.compress_video(src, out))

    assert (info.width, info.height) == (480, 854)
    assert abs(info.bytes - src.stat().st_size) < src.stat().st_size * 0.1


def test_images_become_smaller_webp(samples, tmp_path):
    path, info = run(media.convert_image(samples["photo.png"], tmp_path / "01"))

    assert path.suffix == ".webp"
    assert max(info.width, info.height) == 1440
    assert info.bytes < samples["photo.png"].stat().st_size


def test_thumbnails_from_video_and_image(samples, tmp_path):
    run(media.make_thumbnail(samples["portrait.mp4"], tmp_path / "video.webp"))
    run(media.make_thumbnail(samples["photo.png"], tmp_path / "image.webp"))

    assert streams(tmp_path / "video.webp")["video"]["width"] == 480
    assert streams(tmp_path / "image.webp")["video"]["width"] == 480


def test_keyframes_are_spread_across_the_video(samples, tmp_path):
    frames = run(media.keyframes(samples["portrait.mp4"], 4, tmp_path))
    assert len(frames) == 4
    assert max(streams(frames[0])["video"]["height"], streams(frames[0])["video"]["width"]) == 512


def test_audio_comes_out_as_16khz_mono(samples, tmp_path):
    assert run(media.extract_audio(samples["portrait.mp4"], tmp_path / "a.wav"))
    audio = streams(tmp_path / "a.wav")["audio"]
    assert (audio["sample_rate"], audio["channels"]) == ("16000", 1)


def test_a_silent_video_has_no_audio_to_extract(samples, tmp_path):
    assert not run(media.extract_audio(samples["silent.mp4"], tmp_path / "a.wav"))


def test_a_broken_file_fails_the_post(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    with pytest.raises(PostFailed, match="Could not process the media"):
        run(media.compress_video(broken, tmp_path / "out.mp4"))
