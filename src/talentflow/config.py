"""Runtime configuration, overridable via TALENTFLOW_* environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TALENTFLOW_", env_file=".env", extra="ignore")

    model: str = "claude-opus-4-8"
    max_tokens: int = 4096
    checkpoint_db: str = "talentflow_checkpoints.sqlite"
    random_seed: int = 42

    # Orchestration policy knobs
    shortlist_size: int = 3
    min_qualified_for_shortlist: int = 2
    max_jd_revisions: int = 3
    max_sourcing_rounds: int = 2
    screening_advance_threshold: int = 70


@lru_cache
def get_settings() -> Settings:
    return Settings()
