from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Read from environment variables; `.env.example` documents each one."""

    data_dir: Path = Path("data")
    notion_token: str = ""
    notion_data_source_id: str = ""
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3.5:9b"
    whisper_url: str = "http://whisper:8000"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vcollect.db"
