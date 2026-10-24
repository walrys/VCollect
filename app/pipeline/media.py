"""ffmpeg work: compress videos, convert images, thumbnails, keyframes, audio."""

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.errors import PostFailed

SHORT_SIDE = 720  # videos: the shorter side is capped here, so 1080x1920 becomes 720x1280
IMAGE_LONG_SIDE = 1440
THUMB_SHORT_SIDE = 480
LLM_LONG_SIDE = 512
# A source already in the target format below this bitrate is kept as it is (remuxed only).
KEEP_MAX_BITRATE = 2_500_000

# Even dimensions, never upscaled. In the filters, iw and ih are the input size.
SCALE_SHORT_SIDE = (
    "scale=w='if(lte(iw,ih),trunc(min({n},iw)/2)*2,-2)'"
    ":h='if(lte(iw,ih),-2,trunc(min({n},ih)/2)*2)'"
)


@dataclass
class MediaInfo:
    width: int | None
    height: int | None
    duration: float | None
    bytes: int


async def run(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise PostFailed(f"Could not process the media: {args[0]}: {tail[0]}"[:500])
    return stdout.decode(errors="replace")


async def probe(path: Path) -> dict:
    output = await run(
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    )
    return json.loads(output)


async def info(path: Path) -> MediaInfo:
    data = await probe(path)
    video = _first_stream(data, "video")
    duration = data.get("format", {}).get("duration")
    return MediaInfo(
        width=video and video.get("width"),
        height=video and video.get("height"),
        duration=float(duration) if duration else None,
        bytes=path.stat().st_size,
    )


async def compress_video(src: Path, dst: Path) -> MediaInfo:
    """H.264 + AAC MP4, shorter side at most 720 px, ready to stream (faststart)."""
    data = await probe(src)
    video = _first_stream(data, "video")
    if video is None:
        raise PostFailed("Could not process the media: the file has no video stream")
    if _already_fine(data, video):
        await run(
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dst),
        )
    else:
        await run(
            "ffmpeg", "-y", "-v", "error", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", SCALE_SHORT_SIDE.format(n=SHORT_SIDE),
            "-c:v", "libx264", "-crf", "26", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", str(dst),
        )  # fmt: skip
    return await info(dst)


async def convert_image(src: Path, dst_stem: Path) -> tuple[Path, MediaInfo]:
    """WebP at quality 80, longest side at most 1440 px; keeps the original if that is smaller."""
    webp = dst_stem.with_suffix(".webp")
    await run(
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vf", f"scale='min({IMAGE_LONG_SIDE},iw)':'min({IMAGE_LONG_SIDE},ih)'"
               ":force_original_aspect_ratio=decrease",
        "-c:v", "libwebp", "-quality", "80", str(webp),
    )  # fmt: skip
    if webp.stat().st_size >= src.stat().st_size:
        webp.unlink()
        kept = dst_stem.with_suffix(src.suffix.lower())
        shutil.copyfile(src, kept)
        return kept, await info(kept)
    return webp, await info(webp)


async def make_thumbnail(src: Path, dst: Path) -> None:
    """A WebP still, shorter side 480 px, from an image or from a video's first second."""
    data = await probe(src)
    duration = float(data.get("format", {}).get("duration") or 0)
    seek = ["-ss", f"{min(1.0, duration / 2):.2f}"] if _is_video(data) else []
    await run(
        "ffmpeg", "-y", "-v", "error", *seek, "-i", str(src), "-frames:v", "1",
        "-vf", SCALE_SHORT_SIDE.format(n=THUMB_SHORT_SIDE),
        "-c:v", "libwebp", "-quality", "75", str(dst),
    )  # fmt: skip


async def keyframes(video: Path, count: int, dest: Path) -> list[Path]:
    """`count` evenly spaced JPEG frames, longest side 512 px, for the summary model."""
    duration = (await info(video)).duration or 0
    frames = []
    for index in range(count):
        frame = dest / f"frame-{index}.jpg"
        await run(
            "ffmpeg", "-y", "-v", "error", "-ss", f"{duration * (index + 0.5) / count:.2f}",
            "-i", str(video), "-frames:v", "1", "-vf", _fit_long_side(LLM_LONG_SIDE),
            "-q:v", "4", str(frame),
        )  # fmt: skip
        if frame.exists():
            frames.append(frame)
    return frames


async def llm_image(src: Path, dst: Path) -> Path:
    """An image scaled down for the summary model."""
    await run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", _fit_long_side(LLM_LONG_SIDE),
              "-q:v", "4", str(dst))  # fmt: skip
    return dst


async def extract_audio(video: Path, dst: Path) -> bool:
    """16 kHz mono WAV for whisper; False when the video has no sound."""
    if _first_stream(await probe(video), "audio") is None:
        return False
    await run("ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
              "-c:a", "pcm_s16le", str(dst))  # fmt: skip
    return True


def _already_fine(data: dict, video: dict) -> bool:
    audio = _first_stream(data, "audio")
    bitrate = int(data.get("format", {}).get("bit_rate") or 0)
    return (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and min(video.get("width", 0), video.get("height", 0)) <= SHORT_SIDE
        and (audio is None or audio.get("codec_name") == "aac")
        and 0 < bitrate <= KEEP_MAX_BITRATE
    )


def _is_video(data: dict) -> bool:
    duration = float(data.get("format", {}).get("duration") or 0)
    return _first_stream(data, "video") is not None and duration > 0.1


def _first_stream(data: dict, kind: str) -> dict | None:
    return next((s for s in data.get("streams", []) if s.get("codec_type") == kind), None)


def _fit_long_side(n: int) -> str:
    return f"scale='min({n},iw)':'min({n},ih)':force_original_aspect_ratio=decrease"
