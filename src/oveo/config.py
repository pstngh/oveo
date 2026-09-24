from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # The model is told today's date in this zone so it can place dates in the past or
    # future. Alithya's users work in Montréal.
    timezone: str = "America/Toronto"
    secure_cookies: bool = True
    session_days: int = Field(default=30, ge=1, le=90)
    login_attempts: int = Field(default=5, ge=2, le=20)
    login_window_seconds: int = Field(default=900, ge=60, le=3600)
    login_lock_seconds: int = Field(default=900, ge=60, le=86_400)
    # Failed sign-ins per window from one client address, and from all clients, before
    # further attempts are refused without hashing.
    login_client_failure_limit: int = Field(default=20, ge=5, le=1_000)
    login_global_failure_limit: int = Field(default=60, ge=10, le=10_000)
    max_upload_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    max_source_words: int = Field(default=25_000, ge=1000, le=100_000)
    # The latest reference is re-sent with every turn and cannot be compacted away.
    max_reference_words: int = Field(default=25_000, ge=1000, le=100_000)
    context_compaction_tokens: int = Field(default=240_000, ge=1_000, le=260_000)
    context_recent_messages: int = Field(default=12, ge=4, le=100)
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_timeout_seconds: float = Field(default=300.0, ge=10, le=900)
    # Output-token cap for visible chat responses. OpenRouter publishes 128,000 as the
    # pinned model's maximum completion; the default keeps the established 32,000.
    chat_max_completion_tokens: int = Field(default=32_000, ge=1_000, le=128_000)
    # Wall-clock bound for one provider attempt; keep-alives cannot extend it.
    provider_attempt_deadline_seconds: float = Field(default=1_800.0, ge=60, le=7_200)
    provider_metadata_timeout_seconds: float = Field(default=3.0, ge=0.1, le=10)
    provider_retry_attempts: int = Field(default=3, ge=1, le=5)
    # Provider calls in flight at once across all conversations; others wait their turn.
    max_concurrent_provider_calls: int = Field(default=4, ge=1, le=16)
    session_cookie_name: str = "oveo_session"
    csrf_cookie_name: str = "oveo_csrf"
    charles_password_hash: SecretStr | None = None
    yousra_password_hash: SecretStr | None = None

    @field_validator("public_origin")
    @classmethod
    def strip_origin_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA time zone name") from exc
        return value

    @property
    def production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def resolved_error_log_path(self) -> Path:
        return self.error_log_path or self.data_dir / "logs" / "oveo-errors.log"

    @property
    def maintenance_marker(self) -> Path:
        """While this file exists, state-changing API requests get 503 (see deploy)."""

        return self.data_dir / "maintenance-mode"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attachments_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.resolved_error_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache
def get_settings() -> Settings:
    return Settings()
