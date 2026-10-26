"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Address the phone opens (the QR code points here), e.g. a cloudflared tunnel URL.
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"))
    # Access key: required on every request, carried in the QR code and the printed laptop link.
    # Keeps strangers who find the tunnel address from using the app (and your API credits).
    access_key: str = field(default_factory=lambda: os.getenv("APP_ACCESS_KEY") or secrets.token_urlsafe(9))
    max_upload_bytes: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024))))
