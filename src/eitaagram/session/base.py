"""Per-account session state and the storage contract."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..utils import generate_imei, now

SESSION_FORMAT = 1


class SessionCipher(Protocol):
    """Symmetric cipher applied to the serialized session blob at rest."""

    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, data: bytes) -> bytes: ...


@dataclass(slots=True)
class PeerEntry:
    kind: str  # "user" | "chat" | "channel"
    id: int
    access_hash: int | None = None
    username: str | None = None
    name: str | None = None


@dataclass(slots=True)
class SessionData:
    name: str
    imei: str = field(default_factory=generate_imei)
    phone: str | None = None
    token: str | None = None
    token_expires_at: int | None = None
    user_id: int | None = None
    username: str | None = None
    is_bot: bool = False
    created_at: int = field(default_factory=now)
    updated_at: int = field(default_factory=now)
    peers: dict[tuple[str, int], PeerEntry] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)  # update cursor: pts/qts/date/seq/channels
    extra: dict[str, Any] = field(default_factory=dict)  # free space for applications

    def to_dict(self, *, include_peers: bool = True) -> dict[str, Any]:
        return {
            "v": SESSION_FORMAT,
            "name": self.name,
            "imei": self.imei,
            "phone": self.phone,
            "token": self.token,
            "token_expires_at": self.token_expires_at,
            "user_id": self.user_id,
            "username": self.username,
            "is_bot": self.is_bot,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": self.state,
            "extra": self.extra,
            "peers": [[p.kind, p.id, p.access_hash, p.username, p.name] for p in self.peers.values()]
            if include_peers
            else [],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, name: str | None = None) -> SessionData:
        if raw.get("v") != SESSION_FORMAT:
            raise ValueError(f"unsupported session format: {raw.get('v')!r}")
        data = cls(
            name=name or str(raw.get("name") or "default"),
            imei=str(raw.get("imei") or generate_imei()),
            phone=raw.get("phone"),
            token=raw.get("token"),
            token_expires_at=raw.get("token_expires_at"),
            user_id=raw.get("user_id"),
            username=raw.get("username"),
            is_bot=bool(raw.get("is_bot", False)),
            created_at=int(raw.get("created_at") or now()),
            updated_at=int(raw.get("updated_at") or now()),
            state=dict(raw.get("state") or {}),
            extra=dict(raw.get("extra") or {}),
        )
        for kind, ident, access_hash, username, display in raw.get("peers", []):
            data.peers[(kind, int(ident))] = PeerEntry(kind, int(ident), access_hash, username, display)
        return data


class Session(ABC):
    """State of one account plus load/save plumbing.

    Subclasses only implement three tiny async primitives (``_read`` / ``_write``
    / ``_remove``).  Frequent changes (peer cache, update cursor) just mark the
    session dirty; :meth:`flush` persists them, and critical changes (a new
    token) call :meth:`save` directly.
    """

    def __init__(self, name: str = "default") -> None:
        self.data = SessionData(name=name)
        self._dirty = True
        self._lock = asyncio.Lock()

    # -- storage primitives -------------------------------------------------
    @abstractmethod
    async def _read(self) -> dict[str, Any] | None: ...

    @abstractmethod
    async def _write(self, raw: dict[str, Any]) -> None: ...

    @abstractmethod
    async def _remove(self) -> None: ...

    # -- lifecycle ----------------------------------------------------------
    async def load(self) -> None:
        async with self._lock:
            raw = await self._read()
            if raw is not None:
                self.data = SessionData.from_dict(raw, name=self.data.name)
                self._dirty = False

    async def save(self) -> None:
        async with self._lock:
            self.data.updated_at = now()
            await self._write(self.data.to_dict())
            self._dirty = False

    async def flush(self) -> None:
        if self._dirty:
            await self.save()

    async def delete(self) -> None:
        async with self._lock:
            await self._remove()
            self.data = SessionData(name=self.data.name)
            self._dirty = True

    def mark_dirty(self) -> None:
        self._dirty = True

    # -- convenience --------------------------------------------------------
    @property
    def name(self) -> str:
        return self.data.name

    @property
    def authorized(self) -> bool:
        return bool(self.data.token)

    def cache_peer(self, entry: PeerEntry) -> None:
        key = (entry.kind, entry.id)
        old = self.data.peers.get(key)
        if old == entry:
            return
        if old is not None:
            # keep what we already know when a "min" object gives less information
            entry.access_hash = entry.access_hash if entry.access_hash is not None else old.access_hash
            entry.username = entry.username if entry.username is not None else old.username
            entry.name = entry.name or old.name
            if old == entry:
                return
        self.data.peers[key] = entry
        self._dirty = True

    def get_peer(self, kind: str, ident: int) -> PeerEntry | None:
        return self.data.peers.get((kind, ident))

    def find_username(self, username: str) -> PeerEntry | None:
        wanted = username.lstrip("@").lower()
        for entry in self.data.peers.values():
            if entry.username and entry.username.lower() == wanted:
                return entry
        return None


def dumps(raw: dict[str, Any]) -> bytes:
    return json.dumps(raw, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def loads(blob: bytes) -> dict[str, Any]:
    value = json.loads(blob.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("session blob is not an object")
    return value
