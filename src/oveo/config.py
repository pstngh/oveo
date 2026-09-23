from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OVEO_",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "development"
    public_origin: str = "http://localhost:8000"
    trusted_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    data_dir: Path = Path("data")
    database_url: str = "sqlite+aiosqlite:///data/oveo.sqlite3"
    attachments_dir: Path = Path("data/attachments")
    error_log_path: Path | None = None
    error_log_max_bytes: int = Field(default=5_000_000, ge=100_000, le=100_000_000)
    error_log_backup_count: int = Field(default=5, ge=1, le=20)
    frontend_dir: Path = Path("frontend/dist")
    prompts_dir: Path = Path("prompts")
    secure_cookies: bool = True
    session_days: int = Field(default=30, ge=1, le=90)
    login_attempts: int = Field(default=5, ge=2, le=20)
    login_window_seconds: int = Field(default=900, ge=60, le=3600)
    login_lock_seconds: int = Field(default=900, ge=60, le=86_400)
    max_upload_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    max_source_words: int = Field(default=25_000, ge=1000, le=100_000)
    context_compaction_tokens: int = Field(default=240_000, ge=1_000, le=260_000)
    context_recent_messages: int = Field(default=12, ge=4, le=100)
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-6-luna"
    openrouter_timeout_seconds: float = Field(default=300.0, ge=10, le=900)
    provider_metadata_timeout_seconds: float = Field(default=3.0, ge=0.1, le=10)
    provider_retry_attempts: int = Field(default=3, ge=1, le=5)
    session_cookie_name: str = "oveo_session"
    csrf_cookie_name: str = "oveo_csrf"
    charles_password_hash: SecretStr | None = None
    yousra_password_hash: SecretStr | None = None

    @field_validator("public_origin")
    @classmethod
    def strip_origin_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def resolved_error_log_path(self) -> Path:
        return self.error_log_path or self.data_dir / "logs" / "oveo-errors.log"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attachments_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.resolved_error_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache
def get_settings() -> Settings:
    return Settings()
