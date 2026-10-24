"""Transcripts: the platform's subtitles when there are any, else whisper."""

import re
from pathlib import Path

import httpx

from app.pipeline.errors import TryLater

# Whisper invents words over music and silence; segments it rates as likely
# non-speech are dropped.
MAX_NO_SPEECH_PROB = 0.6
VTT_TAG = re.compile(r"<[^>]+>")


class Transcriber:
    def __init__(
        self, base_url: str, model: str, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.base_url = base_url
        self.model = model
        self.transport = transport

    async def transcribe(self, audio: Path) -> str:
        try:
            async with httpx.AsyncClient(timeout=1800, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/v1/audio/transcriptions",
                    files={"file": (audio.name, audio.read_bytes(), "audio/wav")},
                    data={"model": self.model, "response_format": "verbose_json", "language": "en"},
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise TryLater(f"Whisper failed: {error}") from error
        body = response.json()
        segments = body.get("segments")
        if segments is None:
            return body.get("text", "").strip()
        spoken = [
            s["text"].strip() for s in segments if s.get("no_speech_prob", 0) < MAX_NO_SPEECH_PROB
        ]
        return " ".join(text for text in spoken if text)


def vtt_to_text(vtt: str) -> str:
    """Plain text from WebVTT, without the rolling repeats of YouTube's auto-captions."""
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = VTT_TAG.sub("", raw).strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if line.isdigit() or (lines and lines[-1] == line):
            continue
        lines.append(line)
    return " ".join(lines)
