from contextlib import closing

from app import db


def test_migrate_twice_lands_on_latest_version(tmp_path):
    with closing(db.connect(tmp_path / "test.db")) as conn:
        db.migrate(conn)
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_sqlite_build_has_fts5(tmp_path):
    with closing(db.connect(tmp_path / "test.db")) as conn:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
