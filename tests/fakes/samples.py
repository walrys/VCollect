"""Tiny media files generated with ffmpeg, so the repo holds no binaries."""

import subprocess
from pathlib import Path

SAMPLES = {
    # 1080x1920 is over the 720 px cap, so this one is always re-encoded.
    "portrait.mp4": ["-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=25:duration=2",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                     "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-shortest"],
    # Already H.264/AAC, 480x854 and light: kept as it is.
    "small.mp4": ["-f", "lavfi", "-i", "testsrc=size=480x854:rate=25:duration=2",
                  "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                  "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "64k", "-shortest"],
    "landscape.webm": ["-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=25:duration=2",
                       "-c:v", "libvpx-vp9", "-deadline", "realtime", "-b:v", "1M"],
    "silent.mp4": ["-f", "lavfi", "-i", "testsrc=size=720x1280:rate=25:duration=2",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"],
    "photo.png": ["-f", "lavfi", "-i", "testsrc=size=2000x3000", "-frames:v", "1"],
}  # fmt: skip


def make_samples(folder: Path) -> dict[str, Path]:
    paths = {}
    for name, args in SAMPLES.items():
        path = folder / name
        subprocess.run(["ffmpeg", "-y", "-v", "error", *args, str(path)], check=True)
        paths[name] = path
    return paths
