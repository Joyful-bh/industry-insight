from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    bailian_api_key: SecretStr | None = Field(default=None, validation_alias="DASHSCOPE_API_KEY")
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
    search_max_output_tokens: int = Field(default=8192, ge=1200, le=65536)
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


class Stage2PromptConfig(BaseModel):
    event_extraction: Path


class Stage2Config(BaseModel):
    http_timeout_seconds: int = Field(default=30, ge=5, le=180)
    http_max_retries: int = Field(default=2, ge=0, le=5)
    http_min_interval_per_host_seconds: float = Field(default=1.0, ge=0, le=60)
    max_response_bytes: int = Field(default=15_728_640, ge=1_048_576, le=104_857_600)
    min_usable_text_chars: int = Field(default=500, ge=100, le=10_000)
    max_model_input_chars: int = Field(default=80_000, ge=5_000, le=200_000)
    analysis_model: str = "qwen3.7-flash"
    web_extractor_model: str = "qwen3.7-flash"
    web_extractor_reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "low"
    ocr_model: str = "qwen3.5-ocr"
    analysis_max_output_tokens: int = Field(default=8192, ge=512, le=32_768)
    web_extractor_max_output_tokens: int = Field(default=8192, ge=512, le=32_768)
    prompts: Stage2PromptConfig


class Stage3PromptConfig(BaseModel):
    topic_generation: Path
    topic_reconciliation: Path


class Stage3Config(BaseModel):
    model: str = "qwen3.7-flash"
    event_batch_size: int = Field(default=10, ge=1, le=100)
    max_events_per_run: int = Field(default=200, ge=1, le=1000)
    max_topics_per_event: int = Field(default=3, ge=1, le=5)
    minimum_event_confidence: float = Field(default=0.70, ge=0, le=1)
    generation_max_output_tokens: int = Field(default=8192, ge=512, le=32_768)
    reconciliation_batch_size: int = Field(default=12, ge=2, le=25)
    reconciliation_max_output_tokens: int = Field(default=16384, ge=512, le=32_768)
    prompts: Stage3PromptConfig


class Stage4PromptConfig(BaseModel):
    track_generation: Path
    track_analysis: Path


class Stage4Config(BaseModel):
    model: str = "qwen3.7-flash"
    minimum_supporting_events: int = Field(default=1, ge=1, le=20)
    forbid_region_in_identity: bool = True
    generation_max_output_tokens: int = Field(default=8192, ge=512, le=32_768)
    analysis_max_output_tokens: int = Field(default=8192, ge=512, le=32_768)
    prompts: Stage4PromptConfig


class PocConfig(BaseModel):
    version: str
    scope: ScopeConfig
    bailian: BailianConfig
    budgets: BudgetConfig
    coverage: CoverageConfig
    prompts: PromptConfig
    stage2: Stage2Config
    stage3: Stage3Config
    stage4: Stage4Config


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
        config.stage2.analysis_model = model_override.strip()
        config.stage2.web_extractor_model = model_override.strip()
        config.stage3.model = model_override.strip()
        config.stage4.model = model_override.strip()
    if settings.bailian_base_url and settings.bailian_base_url.strip():
        config.bailian.base_url = settings.bailian_base_url.strip()
    return config
