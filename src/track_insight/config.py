from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRACK_INSIGHT_",
        extra="ignore",
    )

    database_url: str = (
        "postgresql+psycopg://track_insight:track_insight@localhost:5432/track_insight"
    )
    blob_root: Path = Path("data/blob")
    log_level: str = "INFO"
    job_lease_seconds: int = Field(default=300, ge=30, le=86_400)
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: int = Field(default=60, ge=5, le=600)
    llm_max_input_chars: int = Field(default=12000, ge=1000, le=100000)

    def resolved_blob_root(self) -> Path:
        return self.blob_root.expanduser().resolve()

    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
