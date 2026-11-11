"""Sync's posts go through a worker process that starts with a run's first post and exits when
the run ends. The web server, which runs all day, never loads the downloaders, and whatever
memory a run builds up goes back to the system with the worker.

The two talk in JSON lines. The app sends {"post": id, "skip": {site: wall}} for each post and
{"cancel": true} when the user cancels. The worker answers a post with its steps,
{"step": "compressing"}, and then {"done": true} or {"error": {...}}. When the app closes the
worker's input, the worker exits, cancelling the post in hand if the app is shutting down.

`python -m app.pipeline.worker` runs a worker; it reads the same environment as the app.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from app import db
from app.config import Settings
from app.pipeline.errors import LoginNeeded, PostFailed, PostNeedsLogin, TryLater
from app.pipeline.runner import Cancelled

if TYPE_CHECKING:
    from app.pipeline.post import Pipeline

COMMAND = (sys.executable, "-m", "app.pipeline.worker")
STOP_SECONDS = 10  # how long a stopping worker has to tidy up before it is killed
LINE_LIMIT = 1024 * 1024  # longest message either side sends: an error, at most

# Errors that cross the pipe as themselves, most specific first; any other is unexpected.
ERRORS = {
    kind.__name__: kind for kind in (Cancelled, LoginNeeded, PostNeedsLogin, TryLater, PostFailed)
}

logger = logging.getLogger(__name__)


class WorkerError(Exception):
    """An unexpected error in the worker, which has logged its traceback."""


class Worker:
    """Takes each post of a run to the worker process (a runner.Processor)."""

    def __init__(self, data_dir: Path, command: Sequence[str] | None = None):
        self.data_dir = data_dir
        self.command = list(command or COMMAND)
        self._process: asyncio.subprocess.Process | None = None

    async def process(
        self,
        conn: sqlite3.Connection,
        post_id: int,
        on_step: Callable[[str], None],
        skip_sites: Mapping[str, LoginNeeded] = MappingProxyType({}),
    ) -> None:
        """Starts the worker for the run's first post, and again after a worker that died."""
        worker = self._process or await self._start()
        self._send(
            {"post": post_id, "skip": {site: _out(wall) for site, wall in skip_sites.items()}}
        )
        while line := await worker.stdout.readline():
            answer = json.loads(line)
            if "step" in answer:
                # A cancel raises here too; the worker has it already (cancel()) and stops at
                # its next step, so the post's answer is still to come.
                with contextlib.suppress(Cancelled):
                    on_step(answer["step"])
            elif "error" in answer:
                raise _in(answer["error"])
            else:
                return
        code = await worker.wait()
        self._process = None
        if code == -signal.SIGKILL:
            raise TryLater("The Sync worker was killed, perhaps for lack of memory")
        raise TryLater(f"The Sync worker stopped (exit code {code})")

    def cancel(self) -> None:
        if self._process is not None:
            self._send({"cancel": True})

    async def close(self) -> None:
        """Ends the worker: at once between posts; mid-post (the app is shutting down) once it
        has cancelled the post; killed if it takes longer than STOP_SECONDS."""
        worker, self._process = self._process, None
        if worker is None:
            return
        worker.stdin.close()
        try:
            await asyncio.wait_for(worker.wait(), STOP_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                worker.kill()
            await worker.wait()

    async def _start(self) -> asyncio.subprocess.Process:
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT,
        )
        return self._process

    def _send(self, message: dict) -> None:
        # A worker that has died is found out by process(), when its output ends.
        with contextlib.suppress(ConnectionError):
            self._process.stdin.write(json.dumps(message).encode() + b"\n")


async def serve(
    pipeline: "Pipeline",
    db_path: Path,
    commands: asyncio.StreamReader,
    answer: Callable[[dict], None],
) -> None:
    """The worker's side: runs each post it is sent until its input ends."""
    posts: asyncio.Queue[dict | None] = asyncio.Queue()
    cancelled = False
    in_hand: asyncio.Task | None = None

    async def read() -> None:
        nonlocal cancelled
        while line := await commands.readline():
            command = json.loads(line)
            if command.get("cancel"):
                cancelled = True
            else:
                posts.put_nowait(command)
        posts.put_nowait(None)
        if in_hand is not None:
            in_hand.cancel()  # the app is shutting down: the post resumes on the next Sync

    def on_step(step: str) -> None:
        if cancelled:
            raise Cancelled("Cancelled")
        answer({"step": step})

    reader = asyncio.create_task(read())
    try:
        with closing(db.connect(db_path)) as conn:
            while (command := await posts.get()) is not None:
                cancelled = False
                walls = {site: _in(wall) for site, wall in command["skip"].items()}
                in_hand = asyncio.create_task(
                    pipeline.process(conn, command["post"], on_step, walls)
                )
                try:
                    await in_hand
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    break
                except (PostFailed, TryLater) as error:
                    answer({"error": _out(error)})
                except Exception as error:
                    logger.exception("Processing post %s failed", command["post"])
                    answer({"error": _out(error)})
                else:
                    answer({"done": True})
                finally:
                    in_hand = None
    finally:
        reader.cancel()


def _out(error: Exception) -> dict:
    kind = next((name for name, known in ERRORS.items() if isinstance(error, known)), "")
    return {
        "kind": kind,
        "message": str(error),
        "site": getattr(error, "site", ""),
        "needs_login": getattr(error, "needs_login", False),
    }


def _in(error: dict) -> Exception:
    kind = ERRORS.get(error["kind"])
    if kind is None:
        return WorkerError(error["message"])
    if kind is LoginNeeded:
        return LoginNeeded(error["site"], error["message"], error["needs_login"])
    made = kind(error["message"])
    if isinstance(made, TryLater):
        made.needs_login = error["needs_login"]
    return made


def real_pipeline(settings: Settings) -> "Pipeline":
    """The pipeline with the real downloaders, whisper and Ollama, which only a worker loads."""
    from app.notion.client import NotionClient
    from app.pipeline.download import Downloader
    from app.pipeline.post import Pipeline, Services
    from app.pipeline.summarise import Summariser
    from app.pipeline.transcribe import Transcriber

    return Pipeline(
        services=Services(
            downloader=Downloader(settings.cookies_path, settings.max_duration_min),
            transcriber=Transcriber(settings.whisper_url, settings.whisper_model),
            summariser=Summariser(settings.ollama_url, settings.ollama_model),
        ),
        notion=NotionClient(settings.notion_token, settings.notion_data_source_id),
        props=settings.notion_props,
        data_dir=settings.data_dir,
        keyframes=settings.keyframes,
    )


def main(make_pipeline: Callable[[Settings], "Pipeline"] = real_pipeline) -> None:
    """Runs a worker on this process's stdin and stdout. Tests pass a pipeline of fakes."""
    if not stat.S_ISFIFO(os.fstat(0).st_mode):
        # asyncio can only wait on a pipe; on a file or /dev/null it never hears the end.
        sys.exit("The Sync worker takes its posts from the app, on a pipe")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     worker: %(message)s")
    # The app's pipes move off stdin and stdout, so ffmpeg and the downloaders never touch
    # them: what they read is empty, what they print goes to the log.
    commands_fd, answers_fd = os.dup(0), os.dup(1)
    empty = os.open(os.devnull, os.O_RDONLY)
    os.dup2(empty, 0)
    os.close(empty)
    os.dup2(2, 1)
    answers = os.fdopen(answers_fd, "w")

    def answer(message: dict) -> None:
        answers.write(json.dumps(message) + "\n")
        answers.flush()

    async def run() -> None:
        settings = Settings()
        commands = asyncio.StreamReader(limit=LINE_LIMIT)
        await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(commands), os.fdopen(commands_fd, "rb", 0)
        )
        pipeline = make_pipeline(settings)
        try:
            await serve(pipeline, settings.db_path, commands, answer)
        finally:
            await pipeline.notion.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
