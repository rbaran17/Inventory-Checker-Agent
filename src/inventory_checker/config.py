"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _key_file() -> Path:
    return Path(os.getenv("APP_ACCESS_KEY_FILE", Path.home() / ".config" / "inventory-checker" / "access_key"))


def load_or_create_access_key() -> str:
    """``APP_ACCESS_KEY`` if set; otherwise a random key saved on first start and reused after restarts,
    so open laptop tabs and paired phones keep working when the server is restarted."""
    if os.getenv("APP_ACCESS_KEY"):
        return os.environ["APP_ACCESS_KEY"]
    path = _key_file()
    try:
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    key = secrets.token_urlsafe(9)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    path.chmod(0o600)
    return key


@dataclass
class Settings:
    # Address the phone opens (the QR code points here), e.g. a cloudflared tunnel URL.
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"))
    # Access key: required on every request, carried in the QR code and the printed laptop link.
    # Keeps strangers who find the tunnel address from using the app (and your API credits).
    access_key: str = field(default_factory=load_or_create_access_key)
    max_upload_bytes: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024))))
