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
    return _write_new_key(path)


def _write_new_key(path: Path) -> str:
    """Create the key file readable by its owner only from the first byte (no window with default permissions)."""
    key = secrets.token_urlsafe(18)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".new")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    os.replace(tmp, path)
    return key


def rotate_access_key() -> Path:
    """Replace the saved key. Every laptop link, QR code and open session made with the old key stops working."""
    path = _key_file()
    _write_new_key(path)
    return path


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
    # Each scan is a paid model call. The phone sends about 15 a minute while recording, one at a time.
    max_scans_per_minute: int = field(default_factory=lambda: int(os.getenv("MAX_SCANS_PER_MINUTE", "30")))
    max_concurrent_scans: int = field(default_factory=lambda: int(os.getenv("MAX_CONCURRENT_SCANS", "2")))

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
