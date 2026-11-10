from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from .base import Session, SessionCipher, dumps, loads

_ENC_MARK = b"EGENC1\n"


class FileSession(Session):
    """One account in one JSON file (atomic replace, ``0600`` permissions)."""

    def __init__(self, path: str | os.PathLike[str], *, name: str | None = None, cipher: SessionCipher | None = None) -> None:
        self.path = Path(path).expanduser()
        super().__init__(name or self.path.stem)
        self._cipher = cipher

    def _read_sync(self) -> dict[str, Any] | None:
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return None
        if blob.startswith(_ENC_MARK):
            if self._cipher is None:
                raise ValueError(f"{self.path} is encrypted; pass cipher=")
            blob = self._cipher.decrypt(blob[len(_ENC_MARK) :])
        return loads(blob)

    def _write_sync(self, raw: dict[str, Any]) -> None:
        blob = dumps(raw)
        if self._cipher is not None:
            blob = _ENC_MARK + self._cipher.encrypt(blob)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    async def _read(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._read_sync)

    async def _write(self, raw: dict[str, Any]) -> None:
        await asyncio.to_thread(self._write_sync, raw)

    async def _remove(self) -> None:
        await asyncio.to_thread(lambda: self.path.unlink(missing_ok=True))
