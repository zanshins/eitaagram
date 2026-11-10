from __future__ import annotations

from typing import Any

from .base import Session


class MemorySession(Session):
    """Keeps everything in RAM (nothing survives the process)."""

    def __init__(self, name: str = "memory") -> None:
        super().__init__(name)
        self._raw: dict[str, Any] | None = None

    async def _read(self) -> dict[str, Any] | None:
        return self._raw

    async def _write(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    async def _remove(self) -> None:
        self._raw = None
