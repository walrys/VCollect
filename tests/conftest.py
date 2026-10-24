from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import Settings
from app.main import create_app

# Port 9 (discard) refuses connections, so the AI services read as unreachable.
UNREACHABLE = "http://127.0.0.1:9"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        notion_token="",
        notion_data_source_id="",
        ollama_url=UNREACHABLE,
        ollama_model="qwen3.5:9b",
        whisper_url=UNREACHABLE,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def conn(tmp_path):
    with closing(db.connect(tmp_path / "test.db")) as conn:
        db.migrate(conn)
        yield conn
