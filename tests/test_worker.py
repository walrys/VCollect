"""Sync's worker process: the posts of a run go through it over real pipes, and it is gone
once the run ends, so the web server holds neither the downloaders nor what a run used."""

import json
import os
import subprocess
import sys
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.pipeline import worker
from app.pipeline.errors import LoginNeeded, PostFailed, PostNeedsLogin, TryLater
from app.pipeline.runner import Cancelled
from tests.conftest import UNREACHABLE
from tests.fakes.notion import FULL_SCHEMA, FakeNotion
from tests.support import wait_for_sync

DOWNLOADERS = {"yt_dlp", "instaloader", "trafilatura", "lxml", "nh3", "curl_cffi"}


@pytest.fixture
def fake_worker(monkeypatch, settings, samples):
    """Sync's worker becomes tests.fakes.worker; call it to give the worker a setup."""
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))

    def setup(**fields):
        fields = {"files": [str(samples["small.mp4"])], **fields}
        command = (sys.executable, "-m", "tests.fakes.worker", json.dumps(fields))
        monkeypatch.setattr(worker, "COMMAND", command)

    setup()
    return setup


def workers(settings) -> list[int]:
    """The pids of every worker started so far."""
    return [int(path.name) for path in (settings.data_dir / "workers").iterdir()]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_step(client: TestClient, step: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/api/sync").json()["step"] == step:
            return
        time.sleep(0.02)
    raise AssertionError(f"Sync never reached {step!r}")


def test_a_run_goes_through_one_worker_which_ends_with_it(settings, fake_worker):
    fake_worker(errors={"indiegamejoe": ["PostFailed", "Could not download: private"]})
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://youtu.be/dvUGDMxl8OU", caption="synth patch")
    notion.add_row(url="https://www.instagram.com/indiegamejoe", caption="profile")

    with TestClient(create_app(settings, notion=notion)) as client:
        client.post("/api/sync")
        progress = wait_for_sync(client)
        posts = {post["caption"]: post for post in client.get("/api/posts").json()["posts"]}

    assert (progress["done"], progress["failed"], progress["later"]) == (1, 1, 0)
    assert progress["problems"] == ["profile: Could not download: private"]
    assert posts["synth patch"]["status"] == "done"
    assert posts["synth patch"]["summary"] == "A short summary."
    assert posts["profile"]["status"] == "failed"
    [pid] = workers(settings)
    assert not alive(pid)


def test_cancel_reaches_the_post_in_the_worker(settings, fake_worker):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://youtu.be/slow_down_1")

    with TestClient(create_app(settings, notion=notion)) as client:
        client.post("/api/sync")
        wait_for_step(client, "downloading")
        client.post("/api/sync/cancel")
        progress = wait_for_sync(client)

    # Stopped before compressing what it had downloaded, so the post is still new.
    assert progress["stopped"] == "Cancelled"
    assert (progress["done"], progress["handled"], progress["new"]) == (0, 0, 1)
    [pid] = workers(settings)
    assert not alive(pid)


def test_a_worker_that_dies_leaves_its_post_for_later_and_the_run_goes_on(settings, fake_worker):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    notion.add_row(url="https://youtu.be/worker_dies", caption="doomed")  # newest: goes first

    with TestClient(create_app(settings, notion=notion)) as client:
        client.post("/api/sync")
        progress = wait_for_sync(client)

    assert (progress["done"], progress["later"]) == (1, 1)
    assert progress["problems"] == [
        "doomed: The Sync worker was killed, perhaps for lack of memory"
    ]
    assert len(workers(settings)) == 2  # the second post had a fresh worker
    assert not any(alive(pid) for pid in workers(settings))


def test_shutting_down_mid_post_ends_the_worker_and_the_post_waits(settings, fake_worker):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://youtu.be/slow_down_1")

    with TestClient(create_app(settings, notion=notion)) as client:
        client.post("/api/sync")
        wait_for_step(client, "downloading")

    [pid] = workers(settings)
    assert not alive(pid)
    with closing(db.connect(settings.db_path)) as conn:
        assert tuple(conn.execute("SELECT status, media_ready FROM posts").fetchone()) == (
            "Not started",
            0,
        )


@pytest.mark.parametrize(
    "error",
    [
        PostFailed("This account is private"),
        TryLater("Ollama is not answering"),
        PostNeedsLogin("This post is age-restricted"),
        LoginNeeded("Instagram"),
        LoginNeeded("Instagram's API", "Instagram is limiting requests", needs_login=False),
        Cancelled("Cancelled"),
    ],
)
def test_errors_cross_the_pipe_as_themselves(error):
    crossed = worker._in(json.loads(json.dumps(worker._out(error))))

    assert type(crossed) is type(error)
    assert str(crossed) == str(error)
    assert getattr(crossed, "needs_login", None) == getattr(error, "needs_login", None)
    assert getattr(crossed, "site", None) == getattr(error, "site", None)


def test_an_unexpected_error_crosses_as_a_worker_error():
    crossed = worker._in(worker._out(KeyError("thumb_file")))

    assert type(crossed) is worker.WorkerError
    assert str(crossed) == "'thumb_file'"


def test_the_real_worker_starts_and_exits_when_its_input_ends(settings):
    settings.data_dir.mkdir(parents=True)
    env = {**os.environ, "DATA_DIR": str(settings.data_dir)}

    ended = subprocess.run(worker.COMMAND, input=b"", env=env, capture_output=True, timeout=60)

    assert ended.returncode == 0, ended.stderr.decode()
    assert ended.stdout == b""


def test_a_worker_started_without_the_app_says_so_and_ends(settings):
    env = {**os.environ, "DATA_DIR": str(settings.data_dir)}

    ended = subprocess.run(
        worker.COMMAND, stdin=subprocess.DEVNULL, env=env, capture_output=True, timeout=60
    )

    assert ended.returncode == 1
    assert b"takes its posts from the app" in ended.stderr


def test_the_web_server_never_loads_the_downloaders(tmp_path):
    # In a process of its own: this one has loaded them for other tests.
    script = f"""
import sys
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from tests.fakes.notion import FULL_SCHEMA, FakeNotion

settings = Settings(data_dir=sys.argv[1], ollama_url="{UNREACHABLE}", whisper_url="{UNREACHABLE}")
with TestClient(create_app(settings, notion=FakeNotion(FULL_SCHEMA))) as client:
    for path in ("/", "/api/posts", "/api/sync", "/api/processing", "/api/settings", "/api/health"):
        client.get(path).raise_for_status()
print(" ".join(sorted({{name.partition(".")[0] for name in sys.modules}})))
"""
    ran = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "data")],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert ran.returncode == 0, ran.stderr
    assert DOWNLOADERS.isdisjoint(ran.stdout.split())
