"""Friendly wrappers around raw TL dicts (``.raw`` always keeps the original)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .utils import peer_to_chat_id

if TYPE_CHECKING:  # pragma: no cover
    from .client import Client


@dataclass(slots=True)
class User:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str | None = None
    phone: str | None = None
    access_hash: int | None = None
    is_self: bool = False
    is_bot: bool = False
    is_contact: bool = False
    is_deleted: bool = False
    is_min: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def chat_id(self) -> int:
        return self.id

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> User:
        return cls(
            id=int(raw["id"]),
            first_name=raw.get("first_name", ""),
            last_name=raw.get("last_name", ""),
            username=raw.get("username"),
            phone=raw.get("phone"),
            access_hash=raw.get("access_hash"),
            is_self=bool(raw.get("self")),
            is_bot=bool(raw.get("bot")),
            is_contact=bool(raw.get("contact")),
            is_deleted=bool(raw.get("deleted")) or raw.get("_") == "userEmpty",
            is_min=bool(raw.get("min")),
            raw=raw,
        )


@dataclass(slots=True)
class Chat:
    """A private chat, basic group, supergroup or channel.

    ``id`` follows the library-wide convention: users positive, basic groups
    ``-id``, channels/supergroups ``-1000000000000 - id``.
    """

    id: int
    kind: str  # "private" | "group" | "supergroup" | "channel"
    title: str = ""
    username: str | None = None
    members_count: int | None = None
    access_hash: int | None = None
    raw_id: int = 0
    is_forbidden: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_private(self) -> bool:
        return self.kind == "private"

    @classmethod
    def from_user(cls, user: User) -> Chat:
        return cls(
            id=user.id,
            kind="private",
            title=user.full_name or (user.username or ""),
            username=user.username,
            access_hash=user.access_hash,
            raw_id=user.id,
            raw=user.raw,
        )

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Chat:
        name = raw.get("_", "")
        ident = int(raw["id"])
        if name in ("channel", "channelForbidden"):
            kind = "channel" if raw.get("broadcast") else "supergroup"
            return cls(
                id=peer_to_chat_id("channel", ident),
                kind=kind,
                title=raw.get("title", ""),
                username=raw.get("username"),
                members_count=raw.get("participants_count"),
                access_hash=raw.get("access_hash"),
                raw_id=ident,
                is_forbidden=name == "channelForbidden",
                raw=raw,
            )
        return cls(
            id=peer_to_chat_id("chat", ident),
            kind="group",
            title=raw.get("title", ""),
            members_count=raw.get("participants_count"),
            raw_id=ident,
            is_forbidden=name in ("chatForbidden", "chatEmpty"),
            raw=raw,
        )


class Entities:
    """Users and chats that accompany a batch of messages / updates."""

    __slots__ = ("users", "chats")

    def __init__(self, users: list[dict[str, Any]] | None = None, chats: list[dict[str, Any]] | None = None) -> None:
        self.users: dict[int, User] = {}
        self.chats: dict[int, Chat] = {}
        for raw in users or []:
            if isinstance(raw, dict) and "id" in raw:
                user = User.from_raw(raw)
                self.users[user.id] = user
        for raw in chats or []:
            if isinstance(raw, dict) and "id" in raw:
                chat = Chat.from_raw(raw)
                self.chats[chat.id] = chat

    def chat_for_peer(self, peer: dict[str, Any] | None) -> Chat | None:
        if not peer:
            return None
        kind = {"peerUser": "user", "peerChat": "chat", "peerChannel": "channel"}.get(peer.get("_", ""))
        if kind is None:
            return None
        ident = peer.get("user_id") or peer.get("chat_id") or peer.get("channel_id")
        chat_id = peer_to_chat_id(kind, int(ident))
        if kind == "user":
            user = self.users.get(chat_id)
            return Chat.from_user(user) if user else Chat(id=chat_id, kind="private", raw_id=chat_id)
        return self.chats.get(chat_id) or Chat(
            id=chat_id, kind="channel" if kind == "channel" else "group", raw_id=int(ident)
        )


def _media_kind(media: dict[str, Any] | None) -> str | None:
    if not media:
        return None
    name = media.get("_", "")
    if name == "messageMediaPhoto":
        return "photo"
    if name == "messageMediaDocument":
        doc = media.get("document") or {}
        kinds = {a.get("_") for a in doc.get("attributes", []) if isinstance(a, dict)}
        if "documentAttributeSticker" in kinds:
            return "sticker"
        if "documentAttributeAudio" in kinds:
            attrs = [a for a in doc.get("attributes", []) if a.get("_") == "documentAttributeAudio"]
            return "voice" if attrs and attrs[0].get("voice") else "audio"
        if "documentAttributeVideo" in kinds:
            return "animation" if "documentAttributeAnimated" in kinds else "video"
        return "document"
    return {
        "messageMediaGeo": "location",
        "messageMediaGeoLive": "location",
        "messageMediaContact": "contact",
        "messageMediaPoll": "poll",
        "messageMediaWebPage": "webpage",
        "messageMediaVenue": "venue",
        "messageMediaDice": "dice",
    }.get(name, name.removeprefix("messageMedia").lower() or None)


@dataclass(slots=True)
class Message:
    id: int
    chat: Chat
    date: int = 0
    text: str = ""
    outgoing: bool = False
    sender_id: int | None = None
    from_user: User | None = None
    reply_to_message_id: int | None = None
    forward_date: int | None = None
    forward_from_id: int | None = None
    media: str | None = None
    edit_date: int | None = None
    is_service: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    client: Client | None = field(default=None, repr=False, compare=False)
    matches: Any = field(default=None, repr=False, compare=False)  # set by filters.regex

    @property
    def chat_id(self) -> int:
        return self.chat.id

    @property
    def command(self) -> list[str] | None:
        """``["start", "arg1"]`` for ``/start arg1`` (``@bot`` suffix stripped)."""
        if not self.text.startswith("/"):
            return None
        head, *rest = self.text[1:].split()
        return [head.split("@", 1)[0].lower(), *rest]

    @classmethod
    def from_raw(cls, raw: dict[str, Any], entities: Entities, client: Client | None = None) -> Message | None:
        kind = raw.get("_")
        if kind not in ("message", "messageService"):
            return None  # messageEmpty and friends carry nothing useful
        chat = entities.chat_for_peer(raw.get("peer_id"))
        if chat is None:
            return None
        from_peer = raw.get("from_id") or {}
        sender_id = from_peer.get("user_id") or from_peer.get("channel_id") or from_peer.get("chat_id")
        if sender_id is None and chat.kind == "private":
            sender_id = chat.id  # private chat: incoming sender is the peer itself
        if from_peer.get("_") == "peerChannel" and sender_id is not None:
            sender_id = peer_to_chat_id("channel", int(sender_id))
        from_user = entities.users.get(int(sender_id)) if sender_id is not None and from_peer.get("_") != "peerChannel" else None
        fwd = raw.get("fwd_from") or {}
        fwd_from = fwd.get("from_id") or {}
        reply = raw.get("reply_to") or {}
        return cls(
            id=int(raw["id"]),
            chat=chat,
            date=int(raw.get("date", 0)),
            text=raw.get("message", "") if kind == "message" else "",
            outgoing=bool(raw.get("out")),
            sender_id=int(sender_id) if sender_id is not None else None,
            from_user=from_user,
            reply_to_message_id=reply.get("reply_to_msg_id"),
            forward_date=fwd.get("date"),
            forward_from_id=fwd_from.get("user_id") or fwd_from.get("channel_id"),
            media=_media_kind(raw.get("media")),
            edit_date=raw.get("edit_date"),
            is_service=kind == "messageService",
            raw=raw,
            client=client,
        )

    def _need_client(self) -> Client:
        if self.client is None:
            raise RuntimeError("this Message is not bound to a Client")
        return self.client

    async def reply(self, text: str, *, quote: bool = True, **kwargs: Any) -> Message:
        return await self._need_client().send_message(
            self.chat.id, text, reply_to=self.id if quote else None, **kwargs
        )

    async def edit(self, text: str, **kwargs: Any) -> Message:
        return await self._need_client().edit_message_text(self.chat.id, self.id, text, **kwargs)

    async def delete(self, *, revoke: bool = True) -> int:
        return await self._need_client().delete_messages(self.chat.id, [self.id], revoke=revoke)

    async def forward(self, to_chat: int | str) -> list[Message]:
        return await self._need_client().forward_messages(to_chat, self.chat.id, [self.id])

    async def mark_read(self) -> bool:
        return await self._need_client().read_history(self.chat.id, max_id=self.id)


@dataclass(slots=True)
class Dialog:
    chat: Chat
    top_message: Message | None
    unread_count: int = 0
    unread_mentions: int = 0
    pinned: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class SentCode:
    phone: str
    phone_code_hash: str
    type: str  # "sms" | "app" | "call" | "flash_call" | "unknown"
    length: int | None = None
    timeout: int | None = None
    next_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_raw(cls, phone: str, raw: dict[str, Any]) -> SentCode:
        sent_type = raw.get("type") or {}
        name = str(sent_type.get("_", "")).removeprefix("auth.sentCodeType")
        kind = {"Sms": "sms", "App": "app", "Call": "call", "FlashCall": "flash_call"}.get(name, name.lower() or "unknown")
        next_type = raw.get("next_type")
        next_name = None
        if isinstance(next_type, dict):
            next_name = str(next_type.get("_", "")).removeprefix("auth.codeType").lower() or None
        return cls(
            phone=phone,
            phone_code_hash=str(raw.get("phone_code_hash", "")),
            type=kind,
            length=sent_type.get("length"),
            timeout=raw.get("timeout"),
            next_type=next_name,
            raw=raw,
        )


@dataclass(slots=True)
class DeletedMessages:
    ids: list[int]
    chat_id: int | None = None  # set for channels / supergroups, unknown for private chats and basic groups
