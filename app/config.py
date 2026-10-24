from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings


@dataclass(frozen=True)
class NotionProps:
    """Names of the Notion properties VCollect reads and writes."""

    url: str
    caption: str
    tags: str
    status: str
    error: str
    summary: str
    ai_tags: str


class Settings(BaseSettings):
    """Read from environment variables; `.env.example` documents each one."""

    data_dir: Path = Path("data")
    notion_token: str = ""
    notion_data_source_id: str = ""
    notion_prop_url: str = "URL"
    notion_prop_caption: str = "Caption"
    notion_prop_tags: str = "Tags"
    notion_prop_status: str = "Status"
    notion_prop_error: str = "Error"
    notion_prop_summary: str = "Summary"
    notion_prop_ai_tags: str = "AI Tags"
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3.5:9b"
    whisper_url: str = "http://whisper:8000"
    whisper_model: str = "Systran/faster-whisper-small.en"
    keyframes: int = 4
    max_duration_min: int = 0  # 0 = no limit
    disk_cap_gb: float = 100

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vcollect.db"

    @property
    def cookies_path(self) -> Path:
        """Netscape cookies.txt with the Instagram and Reddit sessions."""
        return self.data_dir / "cookies.txt"

    @property
    def notion_configured(self) -> bool:
        return bool(self.notion_token and self.notion_data_source_id)

    @property
    def notion_props(self) -> NotionProps:
        return NotionProps(
            url=self.notion_prop_url,
            caption=self.notion_prop_caption,
            tags=self.notion_prop_tags,
            status=self.notion_prop_status,
            error=self.notion_prop_error,
            summary=self.notion_prop_summary,
            ai_tags=self.notion_prop_ai_tags,
        )
