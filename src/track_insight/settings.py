from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        default="postgresql+psycopg://track_insight:track_insight@localhost:5432/track_insight",
        validation_alias="TRACK_INSIGHT_DATABASE_URL",
    )
    log_level: str = Field(default="INFO", validation_alias="TRACK_INSIGHT_LOG_LEVEL")
    log_file: Path = Field(
        default=Path("logs/track-insight.jsonl"), validation_alias="TRACK_INSIGHT_LOG_FILE"
    )
    log_max_bytes: int = Field(
        default=10_485_760, ge=1_048_576, validation_alias="TRACK_INSIGHT_LOG_MAX_BYTES"
    )
    log_backup_count: int = Field(
        default=5, ge=1, le=100, validation_alias="TRACK_INSIGHT_LOG_BACKUP_COUNT"
    )
    job_lease_seconds: int = Field(
        default=300, ge=30, le=86_400, validation_alias="TRACK_INSIGHT_JOB_LEASE_SECONDS"
    )
    bailian_api_key: SecretStr | None = Field(
        default=None, validation_alias="DASHSCOPE_API_KEY"
    )
    bailian_model: str | None = Field(default=None, validation_alias="BAILIAN_MODEL")
    bailian_base_url: str | None = Field(default=None, validation_alias="BAILIAN_BASE_URL")


class ScopeConfig(BaseModel):
    regions: list[str] = Field(min_length=1)
    industry_scopes: list[str] = Field(min_length=1)
    start_date: date
    end_date: date


class BailianConfig(BaseModel):
    base_url: str
    model: str
    review_batch_size: int = Field(default=10, ge=1, le=50)
    timeout_seconds: int = Field(default=120, ge=5, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_concurrency: int = Field(default=1, ge=1, le=8)


class BudgetConfig(BaseModel):
    max_work_packages: int = Field(ge=1, le=50)
    max_searches_per_work_package: int = Field(ge=1, le=50)
    max_candidates_per_work_package: int = Field(ge=1, le=1000)


class CoverageConfig(BaseModel):
    required_source_classes: list[str]
    dominant_domain_warning_ratio: float = Field(gt=0, le=1)


class PromptConfig(BaseModel):
    planning: Path
    search_review: Path


class PocConfig(BaseModel):
    version: str
    scope: ScopeConfig
    bailian: BailianConfig
    budgets: BudgetConfig
    coverage: CoverageConfig
    prompts: PromptConfig


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_poc_config(path: Path = Path("config/poc.yaml")) -> PocConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = PocConfig.model_validate(payload)
    settings = get_settings()
    model_override = settings.bailian_model
    if model_override and model_override.strip():
        config.bailian.model = model_override.strip()
    if settings.bailian_base_url and settings.bailian_base_url.strip():
        config.bailian.base_url = settings.bailian_base_url.strip()
    return config
