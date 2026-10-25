"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("INVENTORY_DATA_DIR", "data")))
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "https://localhost:8443"))
    # "dev" trusts an X-User header / cookie. Production must sit behind the firm's SSO
    # (e.g. Entra ID via an authenticating reverse proxy that sets X-User after MFA).
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "dev"))
    trusted_user_header: str = field(default_factory=lambda: os.getenv("TRUSTED_USER_HEADER", "X-User"))
    retention_years: int = field(default_factory=lambda: int(os.getenv("EVIDENCE_RETENTION_YEARS", "7")))
    max_upload_bytes: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_BYTES", str(80 * 1024 * 1024))))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "observations.sqlite3"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"
