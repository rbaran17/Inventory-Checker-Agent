"""Persistence: SQLite metadata, write-once evidence storage, append-only hash-chained audit trail.

The pilot runs on a single node. ``EvidenceStore`` is the seam for the firm's object
storage with retention lock (e.g. Azure Blob immutable storage); the local implementation
mimics WORM semantics by refusing overwrites and deletes and marking files read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import (
    Device,
    Evidence,
    ListingItem,
    Record,
    ReviewEvent,
    Session,
    Suggestion,
    Zone,
    utcnow,
)


class WormViolation(Exception):
    """Raised on any attempt to overwrite or delete write-once evidence."""


class EvidenceStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> str:
        """Store bytes once under ``key`` and return a storage URI."""

    @abstractmethod
    def get(self, uri: str) -> bytes: ...

    def delete(self, uri: str) -> None:
        raise WormViolation("Evidence is write-once and cannot be deleted within retention")


class LocalWormEvidenceStore(EvidenceStore):
    def __init__(self, root: Path, retention_years: int = 7):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.retention_years = retention_years

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace("..", "_")
        return self.root / safe

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError as exc:
            raise WormViolation(f"Evidence object {key} already exists") from exc
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return f"worm://local/{path.name}"

    def get(self, uri: str) -> bytes:
        name = uri.rsplit("/", 1)[-1]
        return self._path(name).read_bytes()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zones (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listing (session_id TEXT NOT NULL, sku TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (session_id, sku));
CREATE TABLE IF NOT EXISTS records (id TEXT NOT NULL, version INTEGER NOT NULL, session_id TEXT NOT NULL,
    type TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (id, version));
CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sha256 TEXT NOT NULL,
    data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_links (evidence_id TEXT NOT NULL, record_id TEXT NOT NULL,
    PRIMARY KEY (evidence_id, record_id));
CREATE TABLE IF NOT EXISTS suggestions (id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, record_id TEXT NOT NULL,
    data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, token_hash TEXT,
    data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pairings (token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    expires_at TEXT NOT NULL, used_at TEXT, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, session_id TEXT,
    user TEXT NOT NULL, action TEXT NOT NULL, entity TEXT, details TEXT NOT NULL, prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence
    BEGIN SELECT RAISE(ABORT, 'evidence metadata is write-once'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence
    BEGIN SELECT RAISE(ABORT, 'evidence metadata is write-once'); END;
CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records
    BEGIN SELECT RAISE(ABORT, 'record versions are immutable'); END;
"""

GENESIS_HASH = "0" * 64


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Database:
    """Thin, thread-safe SQLite wrapper storing pydantic models as JSON."""

    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ sessions
    def save_session(self, s: Session) -> None:
        self._q("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (s.id, s.model_dump_json()))

    def get_session(self, session_id: str) -> Session | None:
        rows = self._q("SELECT data FROM sessions WHERE id = ?", (session_id,))
        return Session.model_validate_json(rows[0][0]) if rows else None

    def list_sessions(self) -> list[Session]:
        return [Session.model_validate_json(r[0]) for r in self._q("SELECT data FROM sessions")]

    # ------------------------------------------------------------------ zones / listing
    def save_zone(self, z: Zone) -> None:
        self._q("INSERT OR REPLACE INTO zones VALUES (?, ?, ?)", (z.id, z.session_id, z.model_dump_json()))

    def list_zones(self, session_id: str) -> list[Zone]:
        rows = self._q("SELECT data FROM zones WHERE session_id = ?", (session_id,))
        return [Zone.model_validate_json(r[0]) for r in rows]

    def save_listing_item(self, session_id: str, item: ListingItem) -> None:
        self._q(
            "INSERT OR REPLACE INTO listing VALUES (?, ?, ?)", (session_id, item.sku, item.model_dump_json())
        )

    def get_listing_item(self, session_id: str, sku: str) -> ListingItem | None:
        rows = self._q("SELECT data FROM listing WHERE session_id = ? AND sku = ?", (session_id, sku))
        return ListingItem.model_validate_json(rows[0][0]) if rows else None

    def list_listing(self, session_id: str) -> list[ListingItem]:
        rows = self._q("SELECT data FROM listing WHERE session_id = ? ORDER BY sku", (session_id,))
        return [ListingItem.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------ records
    def insert_record_version(self, r: Record) -> None:
        self._q(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
            (r.id, r.version, r.session_id, r.type.value, r.model_dump_json()),
        )

    def get_record(self, record_id: str, version: int | None = None) -> Record | None:
        if version is None:
            rows = self._q(
                "SELECT data FROM records WHERE id = ? ORDER BY version DESC LIMIT 1", (record_id,)
            )
        else:
            rows = self._q("SELECT data FROM records WHERE id = ? AND version = ?", (record_id, version))
        return Record.model_validate_json(rows[0][0]) if rows else None

    def record_history(self, record_id: str) -> list[Record]:
        rows = self._q("SELECT data FROM records WHERE id = ? ORDER BY version", (record_id,))
        return [Record.model_validate_json(r[0]) for r in rows]

    def list_current_records(self, session_id: str, type_: str | None = None) -> list[Record]:
        sql = (
            "SELECT r.data FROM records r JOIN (SELECT id, MAX(version) v FROM records WHERE session_id = ? "
            "GROUP BY id) m ON r.id = m.id AND r.version = m.v"
        )
        params: list[Any] = [session_id]
        if type_:
            sql += " WHERE r.type = ?"
            params.append(type_)
        sql += " ORDER BY r.rowid"
        return [Record.model_validate_json(r[0]) for r in self._q(sql, params)]

    # ------------------------------------------------------------------ evidence
    def insert_evidence(self, e: Evidence) -> None:
        stored = e.model_copy(update={"record_ids": []})  # links live in their own table
        self._q("INSERT INTO evidence VALUES (?, ?, ?, ?)", (e.id, e.session_id, e.sha256, stored.model_dump_json()))

    def _with_links(self, e: Evidence) -> Evidence:
        links = self._q("SELECT record_id FROM evidence_links WHERE evidence_id = ?", (e.id,))
        return e.model_copy(update={"record_ids": [r[0] for r in links]})

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        rows = self._q("SELECT data FROM evidence WHERE id = ?", (evidence_id,))
        return self._with_links(Evidence.model_validate_json(rows[0][0])) if rows else None

    def list_evidence(self, session_id: str) -> list[Evidence]:
        rows = self._q("SELECT data FROM evidence WHERE session_id = ? ORDER BY rowid", (session_id,))
        return [self._with_links(Evidence.model_validate_json(r[0])) for r in rows]

    def find_evidence_by_hash(self, session_id: str, sha: str) -> Evidence | None:
        rows = self._q("SELECT data FROM evidence WHERE session_id = ? AND sha256 = ?", (session_id, sha))
        return self._with_links(Evidence.model_validate_json(rows[0][0])) if rows else None

    def link_evidence(self, evidence_id: str, record_id: str) -> None:
        self._q("INSERT OR IGNORE INTO evidence_links VALUES (?, ?)", (evidence_id, record_id))

    def unlink_evidence(self, evidence_id: str, record_id: str) -> None:
        self._q("DELETE FROM evidence_links WHERE evidence_id = ? AND record_id = ?", (evidence_id, record_id))

    def evidence_for_record(self, record_id: str) -> list[Evidence]:
        rows = self._q("SELECT evidence_id FROM evidence_links WHERE record_id = ?", (record_id,))
        return [e for e in (self.get_evidence(r[0]) for r in rows) if e]

    # ------------------------------------------------------------------ suggestions / reviews
    def save_suggestion(self, s: Suggestion) -> None:
        self._q("INSERT INTO suggestions VALUES (?, ?, ?)", (s.id, s.evidence_id, s.model_dump_json()))

    def get_suggestion(self, suggestion_id: str) -> Suggestion | None:
        rows = self._q("SELECT data FROM suggestions WHERE id = ?", (suggestion_id,))
        return Suggestion.model_validate_json(rows[0][0]) if rows else None

    def suggestions_for_evidence(self, evidence_id: str) -> list[Suggestion]:
        rows = self._q("SELECT data FROM suggestions WHERE evidence_id = ?", (evidence_id,))
        return [Suggestion.model_validate_json(r[0]) for r in rows]

    def save_review(self, r: ReviewEvent) -> None:
        self._q("INSERT INTO reviews VALUES (?, ?, ?, ?)", (r.id, r.session_id, r.record_id, r.model_dump_json()))

    def list_reviews(self, session_id: str, record_id: str | None = None) -> list[ReviewEvent]:
        if record_id:
            rows = self._q("SELECT data FROM reviews WHERE record_id = ? ORDER BY rowid", (record_id,))
        else:
            rows = self._q("SELECT data FROM reviews WHERE session_id = ? ORDER BY rowid", (session_id,))
        return [ReviewEvent.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------ devices / pairing
    def save_device(self, d: Device, token_hash: str | None = None) -> None:
        if token_hash is None:
            rows = self._q("SELECT token_hash FROM devices WHERE id = ?", (d.id,))
            token_hash = rows[0][0] if rows else None
        self._q("INSERT OR REPLACE INTO devices VALUES (?, ?, ?, ?)", (d.id, d.session_id, token_hash, d.model_dump_json()))

    def get_device_by_token_hash(self, token_hash: str) -> Device | None:
        rows = self._q("SELECT data FROM devices WHERE token_hash = ?", (token_hash,))
        return Device.model_validate_json(rows[0][0]) if rows else None

    def get_device(self, device_id: str) -> Device | None:
        rows = self._q("SELECT data FROM devices WHERE id = ?", (device_id,))
        return Device.model_validate_json(rows[0][0]) if rows else None

    def list_devices(self, session_id: str) -> list[Device]:
        rows = self._q("SELECT data FROM devices WHERE session_id = ?", (session_id,))
        return [Device.model_validate_json(r[0]) for r in rows]

    def save_pairing(self, token_hash: str, session_id: str, expires_at: str, created_by: str) -> None:
        self._q("INSERT INTO pairings VALUES (?, ?, ?, NULL, ?)", (token_hash, session_id, expires_at, created_by))

    def consume_pairing(self, token_hash: str, now_iso: str) -> tuple[str, str] | None:
        """Atomically mark a pairing token used. Returns (session_id, expires_at) or None."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, expires_at, used_at FROM pairings WHERE token_hash = ?", (token_hash,)
            ).fetchall()
            if not rows or rows[0][2] is not None:
                return None
            self._conn.execute("UPDATE pairings SET used_at = ? WHERE token_hash = ?", (now_iso, token_hash))
            return rows[0][0], rows[0][1]

    # ------------------------------------------------------------------ audit trail
    def append_audit(
        self, user: str, action: str, *, session_id: str | None = None, entity: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        with self._lock:
            prev = self._conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchall()
            prev_hash = prev[0][0] if prev else GENESIS_HASH
            ts = utcnow().isoformat()
            body = json.dumps(
                {"ts": ts, "session_id": session_id, "user": user, "action": action, "entity": entity,
                 "details": details or {}, "prev_hash": prev_hash},
                sort_keys=True, default=str,
            )
            h = sha256_hex(body.encode())
            self._conn.execute(
                "INSERT INTO audit (ts, session_id, user, action, entity, details, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, session_id, user, action, entity, json.dumps(details or {}, sort_keys=True, default=str),
                 prev_hash, h),
            )
            return h

    def list_audit(self, session_id: str | None = None) -> list[dict[str, Any]]:
        cols = ["seq", "ts", "session_id", "user", "action", "entity", "details", "prev_hash", "hash"]
        if session_id:
            rows = self._q("SELECT * FROM audit WHERE session_id = ? ORDER BY seq", (session_id,))
        else:
            rows = self._q("SELECT * FROM audit ORDER BY seq")
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["details"] = json.loads(d["details"])
            out.append(d)
        return out

    def verify_audit_chain(self) -> bool:
        prev_hash = GENESIS_HASH
        for e in self.list_audit():
            body = json.dumps(
                {"ts": e["ts"], "session_id": e["session_id"], "user": e["user"], "action": e["action"],
                 "entity": e["entity"], "details": e["details"], "prev_hash": prev_hash},
                sort_keys=True, default=str,
            )
            if e["prev_hash"] != prev_hash or sha256_hex(body.encode()) != e["hash"]:
                return False
            prev_hash = e["hash"]
        return True
