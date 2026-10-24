import asyncio

from app import db
from app.notion.mirror import AUTO_PULL, Mirror
from tests.fakes.notion import FULL_SCHEMA, PROPS, FakeNotion


def test_settings_start_with_auto_pull_off_and_report_disk_use(client, settings):
    body = client.get("/api/settings").json()

    assert body["auto_pull"] is False
    assert body["auto_pull_minutes"] == 5
    disk = body["disk"]
    assert disk["cap_bytes"] == 100 * 1024**3
    assert disk["sync_stops_at_bytes"] == 95 * 1024**3

    (settings.data_dir / "media" / "1").mkdir()
    (settings.data_dir / "media" / "1" / "01.mp4").write_bytes(b"x" * 10_000)
    after = client.get("/api/settings").json()["disk"]["used_bytes"]
    assert after - disk["used_bytes"] == 10_000


def test_auto_pull_is_remembered(client):
    assert client.patch("/api/settings", json={"auto_pull": True}).json()["auto_pull"] is True
    assert client.get("/api/settings").json()["auto_pull"] is True
    assert client.patch("/api/settings", json={}).json()["auto_pull"] is True
    assert client.patch("/api/settings", json={"auto_pull": False}).json()["auto_pull"] is False


def test_auto_pull_pulls_only_while_on(conn, tmp_path):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    mirror = Mirror(notion, PROPS, tmp_path / "test.db")

    async def run_for(seconds: float) -> None:
        loop = asyncio.create_task(mirror.auto_pull(every=0.01))
        await asyncio.sleep(seconds)
        loop.cancel()

    asyncio.run(run_for(0.1))
    assert notion.query_calls == 0

    with conn:
        db.set_setting(conn, AUTO_PULL, "1")
    asyncio.run(run_for(0.1))
    assert notion.query_calls == 1  # later rounds reuse that pull, as it is under a minute old
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
