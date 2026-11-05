"""Runtime configuration from environment variables (see .env.example)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _db_url(_env("DATABASE_URL", "postgresql+psycopg://postgres@/trafficomm?host=/tmp")))
    storage_dir: str = field(default_factory=lambda: _env("STORAGE_DIR", str(BASE_DIR / "var" / "storage")))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY") or _dev_secret())
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-opus-5-5"))
    max_upload_bytes: int = field(default_factory=lambda: int(_env("MAX_UPLOAD_MB", "50")) * 1024 * 1024)
    max_uncompressed_bytes: int = field(default_factory=lambda: int(_env("MAX_UNCOMPRESSED_MB", "300")) * 1024 * 1024)
    max_rows: int = field(default_factory=lambda: int(_env("MAX_ROWS_PER_SHEET", "200000")))
    max_cols: int = field(default_factory=lambda: int(_env("MAX_COLS_PER_SHEET", "250")))
    download_link_ttl: int = field(default_factory=lambda: int(_env("DOWNLOAD_LINK_TTL_SECONDS", "600")))
    job_timeout: int = field(default_factory=lambda: int(_env("JOB_TIMEOUT_SECONDS", "900")))
    job_memory_mb: int = field(default_factory=lambda: int(_env("JOB_MEMORY_MB", "6144")))
    job_max_attempts: int = field(default_factory=lambda: int(_env("JOB_MAX_ATTEMPTS", "3")))
    retention_days_default: int = field(default_factory=lambda: int(_env("RETENTION_DAYS", "180")))
    soffice: str = field(default_factory=lambda: _env("SOFFICE_BIN", "soffice"))
    cookie_secure: bool = field(default_factory=lambda: _env("COOKIE_SECURE", "0") == "1")

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


def _db_url(url: str) -> str:
    """Hosting providers hand out postgres:// URLs; use the installed psycopg 3 driver."""
    for pre in ("postgres://", "postgresql://"):
        if url.startswith(pre):
            return "postgresql+psycopg://" + url[len(pre):]
    return url


def _dev_secret() -> str:
    p = BASE_DIR / "var" / ".dev_secret"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(secrets.token_hex(32))
    return p.read_text().strip()


settings = Settings()
