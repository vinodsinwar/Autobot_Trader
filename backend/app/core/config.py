"""Application configuration.

Everything operational (broker keys, LLM/STT providers, risk limits) lives in the
database `settings` table and is editable from the dashboard. This module only
holds bootstrap configuration: where the DB is, the encryption master key, and
which background services to start.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # .env is looked up in both the process cwd and the repo root, so the app
    # finds it whether launched from the repo root, backend/, or Docker.
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_prefix="AUTOBOT_", extra="ignore"
    )

    # --- storage ---
    database_url: str = "sqlite+aiosqlite:///./autobot.db"

    # --- security ---
    # Fernet key for encrypting secrets at rest (broker keys, telethon session, LLM keys).
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    master_key: str = ""
    # Secret for signing dashboard JWTs. If empty, derived from master_key.
    jwt_secret: str = ""
    jwt_ttl_minutes: int = 12 * 60
    # Initial dashboard password (hashed on first boot into the settings table).
    admin_password: str = "change-me"

    # --- service toggles (all off by default: safe to boot with no credentials) ---
    enable_telegram: bool = False
    enable_youtube: bool = False
    enable_tracker: bool = False
    enable_scrip_sync: bool = False

    # --- http ---
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["http://localhost:5173"]

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
