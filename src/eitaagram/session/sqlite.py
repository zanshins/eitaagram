from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..utils import now
from .base import Session, SessionCipher, dumps, loads
from .file import _ENC_MARK

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY,
    blob BLOB NOT NULL,
    updated_at INTEGER NOT NULL
)
"""


class SQLiteSession(Session):
    """Many accounts in one SQLite file, one row per ``name``.

    ``SQLiteSession("bots.db", "acc1")`` and ``SQLiteSession("bots.db", "acc2")``
    can live side by side; :meth:`list_names` enumerates stored accounts.
    """

    _locks: dict[str, threading.Lock] = {}

    def __init__(self, path: str | os.PathLike[str], name: str = "default", *, cipher: SessionCipher | None = None) -> None:
        super().__init__(name)
        self.path = Path(path).expanduser()
        self._cipher = cipher
        self._thread_lock = self._locks.setdefault(str(self.path.resolve()), threading.Lock())

    # -- helpers ------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        if not existed:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return conn

    @staticmethod
    def list_names(path: str | os.PathLike[str]) -> list[str]:
        p = Path(path).expanduser()
        if not p.exists():
            return []
        conn = sqlite3.connect(p, timeout=30)
        try:
            conn.execute(_SCHEMA)
            return [row[0] for row in conn.execute("SELECT name FROM accounts ORDER BY name")]
        finally:
            conn.close()

    def _read_sync(self) -> dict[str, Any] | None:
        with self._thread_lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT blob FROM accounts WHERE name=?", (self.data.name,)).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        blob: bytes = row[0]
        if blob.startswith(_ENC_MARK):
            if self._cipher is None:
                raise ValueError(f"account {self.data.name!r} is encrypted; pass cipher=")
            blob = self._cipher.decrypt(blob[len(_ENC_MARK) :])
        return loads(blob)

    def _write_sync(self, raw: dict[str, Any]) -> None:
        blob = dumps(raw)
        if self._cipher is not None:
            blob = _ENC_MARK + self._cipher.encrypt(blob)
        with self._thread_lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO accounts(name, blob, updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
                        (self.data.name, blob, now()),
                    )
            finally:
                conn.close()

    def _remove_sync(self) -> None:
        with self._thread_lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute("DELETE FROM accounts WHERE name=?", (self.data.name,))
            finally:
                conn.close()

    async def _read(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._read_sync)

    async def _write(self, raw: dict[str, Any]) -> None:
        await asyncio.to_thread(self._write_sync, raw)

    async def _remove(self) -> None:
        await asyncio.to_thread(self._remove_sync)
