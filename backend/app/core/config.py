from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://navigator:navigator@localhost:5432/navigator"
    )
    cors_origins: list[str] = ["http://localhost:3000"]
    data_dir: Path = ROOT_DIR / "data"

    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"

    voyage_api_key: str = ""
    embedding_model: str = "voyage-code-4"
    embedding_dim: int = 1024

    max_source_files: int = 5000
    max_clone_mb: int = 200
    ingest_timeout_s: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
