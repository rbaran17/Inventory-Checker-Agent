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
    # Address the phone opens (the QR code points here), e.g. a cloudflared tunnel URL. If unset, it is
    # discovered from cloudflared's metrics server (TUNNEL_METRICS_URL), else the address the laptop used.
    public_base_url: str | None = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL") or None)
    tunnel_metrics_url: str | None = field(default_factory=lambda: os.getenv("TUNNEL_METRICS_URL") or None)
    # Access key: required on every request, carried in the QR code and the printed laptop link.
    # Keeps strangers who find the tunnel address from using the app (and your API credits).
    access_key: str = field(default_factory=load_or_create_access_key)
    max_upload_bytes: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024))))

    def resolve_public_url(self) -> str | None:
        """Configured public address, or the quick tunnel's (cached once found)."""
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if self.tunnel_metrics_url:
            from .tunnel import discover_quick_tunnel

            found = discover_quick_tunnel(self.tunnel_metrics_url)
            if found:
                self.public_base_url = found
                return found
        return None
