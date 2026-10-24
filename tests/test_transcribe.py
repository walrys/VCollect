import asyncio

import httpx
import pytest

from app.pipeline.errors import TryLater
from app.pipeline.transcribe import Transcriber, vtt_to_text

YOUTUBE_AUTO_CAPTIONS = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
so<00:00:00.500><c> today</c><00:00:01.000><c> we</c>

00:00:02.000 --> 00:00:02.010 align:start position:0%
so today we

00:00:02.010 --> 00:00:04.000 align:start position:0%
so today we
make<00:00:02.500><c> pasta</c>
"""


def test_auto_captions_lose_timings_and_rolling_repeats():
    assert vtt_to_text(YOUTUBE_AUTO_CAPTIONS) == "so today we make pasta"


def transcribe(tmp_path, handler):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    transcriber = Transcriber("http://whisper:8000", "small.en", httpx.MockTransport(handler))
    return asyncio.run(transcriber.transcribe(audio))


def test_segments_that_are_likely_not_speech_are_dropped(tmp_path):
    def handler(request):
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"verbose_json" in request.content
        return httpx.Response(
            200,
            json={
                "text": "you Hello there.",
                "segments": [
                    {"text": " you", "no_speech_prob": 0.9},
                    {"text": " Hello there.", "no_speech_prob": 0.05},
                ],
            },
        )

    assert transcribe(tmp_path, handler) == "Hello there."


def test_whisper_being_down_means_try_later(tmp_path):
    with pytest.raises(TryLater, match="Whisper failed"):
        transcribe(tmp_path, lambda request: httpx.Response(503))


def test_a_server_error_means_no_speech(tmp_path):
    # speaches fails this way on long audio with no speech in it.
    assert transcribe(tmp_path, lambda request: httpx.Response(500)) == ""
