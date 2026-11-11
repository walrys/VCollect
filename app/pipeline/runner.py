"""The Sync job: one run at a time, one post at a time, in the background. A Retry on the
Processing page, or a preview's Download video, joins the running job, or starts one of its
own. The job runs in the web server; the posts themselves go through a Processor, which in the
app is a worker process that lives only as long as a run (see worker.py)."""

import asyncio
import contextlib
import logging
import sqlite3
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app import db
from app.notion.mirror import Mirror
from app.notion.pull import count_new
from app.pipeline.errors import LoginNeeded, PostFailed, TryLater
from app.pipeline.urls import index_links

MAX_PROBLEMS = 50
# Sync stops downloading once DATA_DIR reaches this share of DISK_CAP_GB.
DISK_STOP_SHARE = 0.95

logger = logging.getLogger(__name__)


class Cancelled(TryLater):
    """Raised at the next step boundary once the user cancels; the post stays Not started."""


class Processor(Protocol):
    """Takes one post through the pipeline: post.Pipeline itself, or worker.Worker, which hands
    it to a worker process."""

    data_dir: Path

    async def process(
        self,
        conn: sqlite3.Connection,
        post_id: int,
        on_step: Callable[[str], None],
        skip_sites: Mapping[str, LoginNeeded],
    ) -> None:
        """Raises PostFailed once the row is marked Failed, or TryLater to leave it for later."""

    def cancel(self) -> None:
        """The user cancelled: the post in hand stops at its next step."""

    async def close(self) -> None:
        """The run is over."""


@dataclass
class Progress:
    running: bool = False
    retry: bool = False  # started by a Retry: only the rows retried, without a pull
    video: bool = False  # started by a preview's Download video, without a pull
    total: int = 0
    handled: int = 0
    done: int = 0
    failed: int = 0
    later: int = 0  # left Not started for the next Sync
    current: str = ""  # the post in hand: its caption, else its link
    step: str = ""
    problems: list[str] = field(default_factory=list)
    stopped: str = ""  # why the run ended early, if it did
    started_at: str | None = None
    finished_at: str | None = None


class Syncer:
    def __init__(self, processor: Processor, mirror: Mirror, db_path: Path, disk_cap_gb: float):
        self.processor = processor
        self.mirror = mirror
        self.db_path = db_path
        self.disk_cap_bytes = disk_cap_gb * 1024**3
        self.progress = Progress()
        self._task: asyncio.Task | None = None
        self._cancelled = False
        self._queue: deque[int] = deque()  # the posts this run has still to process
        self._in_hand: int | None = None  # the post this run is processing

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Starts a Sync unless a run is already going."""
        if not self.running:
            self._begin(pull=True)

    async def retry_now(self, post_ids: list[int]) -> list[int]:
        """Processes the chosen rows at once: next in the running job, else in a run of their
        own, which skips the pull and leaves every other row for the next Sync. Failed rows are
        set back to Not started first (Mirror.retry); rows already Not started, such as those a
        Sync left for later, go as they are. Returns the posts it will process: neither the one
        in hand nor rows that are Done or gone from Notion."""
        if not post_ids:
            return []
        await self.mirror.retry(post_ids)
        with closing(db.connect(self.db_path)) as conn:
            marks = ",".join("?" * len(post_ids))
            waiting = {
                post_id
                for (post_id,) in conn.execute(
                    "SELECT id FROM posts WHERE notion_state = 'synced'"
                    f" AND status = 'Not started' AND id IN ({marks})",
                    post_ids,
                )
            }
        waiting.discard(self._in_hand)  # being processed already
        chosen = [post_id for post_id in post_ids if post_id in waiting]
        if chosen:
            if not self.running:
                self._begin(pull=False)
            self._enqueue(chosen, first=True)
        return chosen

    def download_video(self, post_id: int) -> bool:
        """Downloads the video of a preview at once: next in the running job, else in a run of
        its own. Until it lands, every Sync tries again. False when the post is no preview."""
        with closing(db.connect(self.db_path)) as conn, conn:
            asked = conn.execute(
                "UPDATE posts SET want_video = 1"
                " WHERE id = ? AND kind = 'preview' AND media_ready = 1",
                (post_id,),
            ).rowcount
        if asked and post_id != self._in_hand:
            if not self.running:
                self._begin(pull=False, video=True)
            self._enqueue([post_id], first=True)
        return bool(asked)

    def cancel(self) -> None:
        self._cancelled = True
        self.processor.cancel()

    async def stop(self) -> None:
        """For shutdown: the interrupted post resumes on the next Sync."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task  # until the processor has closed

    def status(self) -> dict:
        with closing(db.connect(self.db_path)) as conn:
            return {**asdict(self.progress), "new": count_new(conn)}

    def _begin(self, pull: bool, video: bool = False) -> None:
        self._cancelled = False
        self._queue = deque()
        self.progress = Progress(
            running=True, retry=not (pull or video), video=video, started_at=_now()
        )
        self._task = asyncio.create_task(self._run(pull))

    def _enqueue(self, post_ids: list[int], first: bool = False) -> None:
        """Adds posts to this run: at the back, or with `first` at the front, where posts it
        has queued already move up to."""
        added = [post_id for post_id in post_ids if post_id not in self._queue]
        if first:
            rest = [post_id for post_id in self._queue if post_id not in post_ids]
            self._queue = deque([*post_ids, *rest])
        else:
            self._queue.extend(added)
        self.progress.total += len(added)

    async def _run(self, pull: bool) -> None:
        progress = self.progress
        try:
            if pull:
                await self.mirror.pull(force=True)
            with closing(db.connect(self.db_path)) as conn:
                index_links(conn)
                if pull:
                    waiting = conn.execute(
                        "SELECT id FROM posts WHERE (notion_state = 'synced'"
                        " AND status = 'Not started') OR want_video = 1 ORDER BY saved_at DESC"
                    ).fetchall()
                    self._enqueue([post_id for (post_id,) in waiting])
                blocked_sites: dict[str, LoginNeeded] = {}  # site -> the wall met there
                while self._queue:
                    if self._cancelled:
                        progress.stopped = "Cancelled"
                        break
                    used = await asyncio.to_thread(disk_used, self.processor.data_dir)
                    if used >= DISK_STOP_SHARE * self.disk_cap_bytes:
                        progress.stopped = "Stopped: the data folder is near DISK_CAP_GB"
                        break
                    self._in_hand = self._queue.popleft()
                    await self._one(conn, self._in_hand, blocked_sites)
                    self._in_hand = None
        except Exception as error:
            logger.exception("Sync stopped")
            progress.stopped = f"Sync stopped: {error}"
        finally:
            self._in_hand = None
            await self.processor.close()
            progress.running = False
            progress.current = progress.step = ""
            progress.finished_at = _now()

    async def _one(
        self, conn: sqlite3.Connection, post_id: int, blocked_sites: dict[str, LoginNeeded]
    ) -> None:
        progress = self.progress
        post = conn.execute(
            "SELECT url, user_caption, title FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        label = post["user_caption"] or post["title"] or post["url"] or f"Post {post_id}"
        progress.current, progress.step = label, "starting"
        retry_error = ""  # why the post is left for the next Sync, if it is
        needs_login = False  # and whether a session in cookies.txt would solve it
        try:
            await self.processor.process(conn, post_id, self._on_step, blocked_sites)
            progress.done += 1
        except Cancelled:
            progress.stopped = "Cancelled"
            return  # the post stays new
        except PostFailed as error:
            progress.failed += 1
            self._problem(f"{label}: {error}")
        except LoginNeeded as error:
            progress.later += 1
            retry_error, needs_login = str(error), error.needs_login
            if error.site not in blocked_sites:
                blocked_sites[error.site] = error
                self._problem(str(error))
        except TryLater as error:
            progress.later += 1
            retry_error, needs_login = str(error), error.needs_login
            self._problem(f"{label}: {error}")
        except Exception as error:
            # A bug with one post should not end the run; the post is retried next Sync.
            logger.exception("Processing post %s failed", post_id)
            progress.later += 1
            retry_error = f"Unexpected error: {error}"
            self._problem(f"{label}: unexpected error: {error}")
        _tried(conn, post_id, retry_error, needs_login)
        progress.handled += 1

    def _on_step(self, step: str) -> None:
        if self._cancelled:
            raise Cancelled("Cancelled")
        self.progress.step = step

    def _problem(self, message: str) -> None:
        if len(self.progress.problems) < MAX_PROBLEMS:
            self.progress.problems.append(message)


def _tried(conn: sqlite3.Connection, post_id: int, retry_error: str, needs_login: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE posts SET retry_error = ?, needs_login = ?, tried_at = ? WHERE id = ?",
            (retry_error, int(needs_login), _now(), post_id),
        )


def disk_used(data_dir: Path) -> int:
    """Bytes in DATA_DIR: the database, media, and downloads in progress."""
    total = 0
    for path in data_dir.rglob("*"):
        try:
            total += path.stat().st_size if path.is_file() else 0
        except FileNotFoundError:
            pass  # removed while counting: a Sync tidying up its downloads
    return total


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
