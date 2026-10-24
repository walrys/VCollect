"""The Sync job: one run at a time, one post at a time, in the background."""

import asyncio
import logging
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app import db
from app.notion.mirror import Mirror
from app.pipeline.errors import LoginNeeded, PostFailed, TryLater
from app.pipeline.post import Pipeline

MAX_PROBLEMS = 50
# Sync stops downloading once DATA_DIR reaches this share of DISK_CAP_GB.
DISK_STOP_SHARE = 0.95

logger = logging.getLogger(__name__)


class Cancelled(TryLater):
    """Raised at the next step boundary once the user cancels; the post stays Not started."""


@dataclass
class Progress:
    running: bool = False
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
    def __init__(self, pipeline: Pipeline, mirror: Mirror, db_path: Path, disk_cap_gb: float):
        self.pipeline = pipeline
        self.mirror = mirror
        self.db_path = db_path
        self.disk_cap_bytes = disk_cap_gb * 1024**3
        self.progress = Progress()
        self._task: asyncio.Task | None = None
        self._cancelled = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Starts a run unless one is already going."""
        if self.running:
            return
        self._cancelled = False
        self.progress = Progress(running=True, started_at=_now())
        self._task = asyncio.create_task(self._run())

    def cancel(self) -> None:
        self._cancelled = True

    async def stop(self) -> None:
        """For shutdown: the interrupted post resumes on the next Sync."""
        if self._task is not None:
            self._task.cancel()

    def status(self) -> dict:
        with closing(db.connect(self.db_path)) as conn:
            waiting = conn.execute(
                "SELECT COUNT(*) FROM posts"
                " WHERE notion_state = 'synced' AND status = 'Not started'"
            ).fetchone()[0]
        return {**asdict(self.progress), "new": waiting}

    async def _run(self) -> None:
        progress = self.progress
        try:
            await self.mirror.pull(force=True)
            with closing(db.connect(self.db_path)) as conn:
                self.pipeline.index_links(conn)
                ids = [
                    post_id
                    for (post_id,) in conn.execute(
                        "SELECT id FROM posts WHERE notion_state = 'synced'"
                        " AND status = 'Not started' ORDER BY saved_at DESC"
                    )
                ]
                progress.total = len(ids)
                blocked_sites: set[str] = set()
                for post_id in ids:
                    if self._cancelled:
                        progress.stopped = "Cancelled"
                        break
                    used = await asyncio.to_thread(disk_used, self.pipeline.data_dir)
                    if used >= DISK_STOP_SHARE * self.disk_cap_bytes:
                        progress.stopped = "Stopped: the data folder is near DISK_CAP_GB"
                        break
                    await self._one(conn, post_id, blocked_sites)
        except Exception as error:
            logger.exception("Sync stopped")
            progress.stopped = f"Sync stopped: {error}"
        finally:
            progress.running = False
            progress.current = progress.step = ""
            progress.finished_at = _now()

    async def _one(self, conn: sqlite3.Connection, post_id: int, blocked_sites: set[str]) -> None:
        progress = self.progress
        post = conn.execute(
            "SELECT url, user_caption FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        label = post["user_caption"] or post["url"] or f"Post {post_id}"
        progress.current, progress.step = label, "starting"
        try:
            await self.pipeline.process(conn, post_id, self._on_step, blocked_sites)
            progress.done += 1
        except Cancelled:
            progress.stopped = "Cancelled"
            return
        except PostFailed as error:
            progress.failed += 1
            self._problem(f"{label}: {error}")
        except LoginNeeded as error:
            progress.later += 1
            if error.site not in blocked_sites:
                blocked_sites.add(error.site)
                self._problem(str(error))
        except TryLater as error:
            progress.later += 1
            self._problem(f"{label}: {error}")
        except Exception as error:
            # A bug with one post should not end the run; the post is retried next Sync.
            logger.exception("Processing post %s failed", post_id)
            progress.later += 1
            self._problem(f"{label}: unexpected error: {error}")
        progress.handled += 1

    def _on_step(self, step: str) -> None:
        if self._cancelled:
            raise Cancelled("Cancelled")
        self.progress.step = step

    def _problem(self, message: str) -> None:
        if len(self.progress.problems) < MAX_PROBLEMS:
            self.progress.problems.append(message)


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
