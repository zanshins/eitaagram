"""Peer resolution + access-hash cache (Telethon-style entity handling)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from .errors import PeerNotFound, UsernameNotOccupied
from .session import PeerEntry, Session
from .types import Chat, User
from .utils import chat_id_to_peer, peer_to_chat_id

_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:eitaa\.com|t\.me)/(?P<name>[A-Za-z0-9_]{3,})/?$")
_INVITE_RE = re.compile(r"(?:eitaa\.com|t\.me)/(?:joinchat/|\+)(?P<hash>[A-Za-z0-9_\-]+)")
_USERNAME_RE = re.compile(r"^@?(?P<name>[A-Za-z][A-Za-z0-9_]{2,})$")

Invoke = Callable[..., Awaitable[Any]]

PeerRef = int | str | User | Chat | dict[str, Any]


def parse_invite_hash(text: str) -> str | None:
    match = _INVITE_RE.search(text)
    return match.group("hash") if match else None


def input_peer(kind: str, ident: int, access_hash: int | None) -> dict[str, Any]:
    if kind == "user":
        return {"_": "inputPeerUser", "user_id": ident, "access_hash": access_hash or 0}
    if kind == "chat":
        return {"_": "inputPeerChat", "chat_id": ident}
    return {"_": "inputPeerChannel", "channel_id": ident, "access_hash": access_hash or 0}


class PeerResolver:
    def __init__(self, session: Session, invoke: Invoke) -> None:
        self.session = session
        self._invoke = invoke

    # ------------------------------------------------------------------ cache
    def ingest(self, users: list[Any] | None, chats: list[Any] | None) -> None:
        for raw in users or []:
            if not isinstance(raw, dict) or raw.get("_") != "user":
                continue
            if raw.get("min"):  # "min" objects carry an access_hash that is not reusable
                continue
            self.session.cache_peer(
                PeerEntry(
                    "user",
                    int(raw["id"]),
                    raw.get("access_hash"),
                    raw.get("username"),
                    " ".join(p for p in (raw.get("first_name"), raw.get("last_name")) if p) or None,
                )
            )
        for raw in chats or []:
            if not isinstance(raw, dict):
                continue
            name = raw.get("_")
            if name == "chat":
                self.session.cache_peer(PeerEntry("chat", int(raw["id"]), None, None, raw.get("title")))
            elif name in ("channel", "channelForbidden") and not raw.get("min"):
                if raw.get("access_hash") is None:
                    continue
                self.session.cache_peer(
                    PeerEntry("channel", int(raw["id"]), raw["access_hash"], raw.get("username"), raw.get("title"))
                )

    # ---------------------------------------------------------------- resolve
    async def resolve(self, ref: PeerRef) -> dict[str, Any]:
        """Return an ``InputPeer`` dict for *ref*."""
        if isinstance(ref, dict):
            if str(ref.get("_", "")).startswith("inputPeer"):
                return ref
            raise PeerNotFound(f"cannot use {ref.get('_')!r} as a peer")
        if isinstance(ref, User):
            return await self.resolve(ref.id)
        if isinstance(ref, Chat):
            return await self.resolve(ref.id)
        if isinstance(ref, int):
            return await self._from_chat_id(ref)
        if isinstance(ref, str):
            return await self._from_string(ref.strip())
        raise PeerNotFound(f"unsupported peer reference: {ref!r}")

    async def resolve_user(self, ref: PeerRef) -> dict[str, Any]:
        peer = await self.resolve(ref)
        if peer["_"] == "inputPeerSelf":
            return {"_": "inputUserSelf"}
        if peer["_"] != "inputPeerUser":
            raise PeerNotFound(f"{ref!r} is not a user")
        return {"_": "inputUser", "user_id": peer["user_id"], "access_hash": peer["access_hash"]}

    async def resolve_channel(self, ref: PeerRef) -> dict[str, Any]:
        peer = await self.resolve(ref)
        if peer["_"] != "inputPeerChannel":
            raise PeerNotFound(f"{ref!r} is not a channel or supergroup")
        return {"_": "inputChannel", "channel_id": peer["channel_id"], "access_hash": peer["access_hash"]}

    async def _from_chat_id(self, chat_id: int) -> dict[str, Any]:
        kind, ident = chat_id_to_peer(chat_id)
        if kind == "user" and ident == self.session.data.user_id:
            return {"_": "inputPeerSelf"}
        if kind == "chat":
            return input_peer("chat", ident, None)
        entry = self.session.get_peer(kind, ident)
        if entry is None or entry.access_hash is None:
            raise PeerNotFound(
                f"unknown {kind} {chat_id}: its access hash is not cached yet. Resolve it once via "
                "@username / invite link, or iterate get_dialogs() so the library can learn it."
            )
        return input_peer(kind, ident, entry.access_hash)

    async def _from_string(self, text: str) -> dict[str, Any]:
        lowered = text.lower()
        if lowered in ("me", "self"):
            return {"_": "inputPeerSelf"}
        if text.lstrip("-").isdigit():
            return await self._from_chat_id(int(text))
        match = _LINK_RE.match(text) or _USERNAME_RE.match(text)
        if not match:
            raise PeerNotFound(f"cannot understand peer reference {text!r}")
        username = match.group("name")
        entry = self.session.find_username(username)
        if entry is not None and (entry.access_hash is not None or entry.kind == "chat"):
            return input_peer(entry.kind, entry.id, entry.access_hash)
        try:
            resolved = await self._invoke("contacts.resolveUsername", {"username": username})
        except UsernameNotOccupied as exc:
            raise PeerNotFound(f"username @{username} does not exist") from exc
        self.ingest(resolved.get("users"), resolved.get("chats"))
        peer = resolved.get("peer") or {}
        kind = {"peerUser": "user", "peerChat": "chat", "peerChannel": "channel"}.get(peer.get("_", ""))
        ident = peer.get("user_id") or peer.get("chat_id") or peer.get("channel_id")
        if kind is None or ident is None:
            raise PeerNotFound(f"@{username} did not resolve to a peer")
        return await self._from_chat_id(peer_to_chat_id(kind, int(ident)))
