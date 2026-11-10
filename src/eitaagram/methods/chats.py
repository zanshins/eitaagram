"""Dialogs, chat lookup, joining and leaving."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ..errors import PeerNotFound
from ..peers import PeerRef, parse_invite_hash
from ..types import Chat, Dialog, Entities, Message, User
from ..utils import peer_to_chat_id

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client


class ChatMethods:
    async def get_chat(self: Client, ref: PeerRef) -> Chat:
        """Resolve *ref* (id, @username, link) to a :class:`Chat`."""
        peer = await self.resolver.resolve(ref)
        if peer["_"] == "inputPeerSelf":
            me = await self.get_me()
            return Chat.from_user(me)
        if peer["_"] == "inputPeerUser":
            users = await self.invoke("users.getUsers", {"id": [{"_": "inputUser", "user_id": peer["user_id"], "access_hash": peer["access_hash"]}]})
            if users and users[0].get("_") == "user":
                return Chat.from_user(User.from_raw(users[0]))
            raise PeerNotFound(f"user {peer['user_id']} not found")
        if peer["_"] == "inputPeerChannel":
            full = await self.invoke(
                "channels.getFullChannel",
                {"channel": {"_": "inputChannel", "channel_id": peer["channel_id"], "access_hash": peer["access_hash"]}},
            )
            target = peer_to_chat_id("channel", peer["channel_id"])
        else:
            full = await self.invoke("messages.getFullChat", {"chat_id": peer["chat_id"]})
            target = peer_to_chat_id("chat", peer["chat_id"])
        entities = Entities(full.get("users"), full.get("chats"))
        chat = entities.chats.get(target)
        if chat is None:
            raise PeerNotFound(f"chat {target} not found")
        return chat

    async def get_users(self: Client, refs: list[PeerRef]) -> list[User]:
        inputs = [await self.resolver.resolve_user(r) for r in refs]
        result = await self.invoke("users.getUsers", {"id": inputs})
        return [User.from_raw(u) for u in result if u.get("_") == "user"]

    async def get_dialogs(self: Client, *, limit: int = 0) -> AsyncIterator[Dialog]:
        """Iterate dialogs (also warms the access-hash cache for every peer seen)."""
        yielded = 0
        offset_date, offset_id = 0, 0
        offset_peer: dict[str, Any] = {"_": "inputPeerEmpty"}
        seen: set[int] = set()
        while True:
            page = 100 if not limit else min(100, limit - yielded)
            result = await self.invoke(
                "messages.getDialogs",
                {"offset_date": offset_date, "offset_id": offset_id, "offset_peer": offset_peer, "limit": page, "hash": 0},
            )
            if result.get("_") == "messages.dialogsNotModified" or not result.get("dialogs"):
                return
            entities = Entities(result.get("users"), result.get("chats"))
            messages: dict[tuple[int, int], Message] = {}
            for raw in result.get("messages", []):
                message = Message.from_raw(raw, entities, self)
                if message is not None:
                    messages[(message.chat.id, message.id)] = message
            batch = 0
            last: Dialog | None = None
            for raw in result["dialogs"]:
                chat = entities.chat_for_peer(raw.get("peer"))
                if chat is None or chat.id in seen:
                    continue
                seen.add(chat.id)
                dialog = Dialog(
                    chat=chat,
                    top_message=messages.get((chat.id, raw.get("top_message", 0))),
                    unread_count=raw.get("unread_count", 0),
                    unread_mentions=raw.get("unread_mentions_count", 0),
                    pinned=bool(raw.get("pinned")),
                    raw=raw,
                )
                batch += 1
                last = dialog
                yield dialog
                yielded += 1
                if limit and yielded >= limit:
                    return
            if batch == 0 or last is None or last.top_message is None:
                return
            offset_date, offset_id = last.top_message.date, last.top_message.id
            offset_peer = await self.resolver.resolve(last.chat.id)
            if result.get("_") == "messages.dialogs":  # complete list in one page
                return

    async def join_chat(self: Client, link_or_username: str) -> Chat:
        """Join by invite link (``.../joinchat/HASH``, ``.../+HASH``) or public @username."""
        invite = parse_invite_hash(link_or_username)
        if invite:
            result = await self.invoke("messages.importChatInvite", {"hash": invite})
        else:
            channel = await self.resolver.resolve_channel(link_or_username)
            result = await self.invoke("channels.joinChannel", {"channel": channel})
        entities = Entities(result.get("users"), result.get("chats")) if isinstance(result, dict) else Entities()
        if entities.chats:
            return next(iter(entities.chats.values()))
        raise PeerNotFound("joined, but the server did not return the chat")

    async def leave_chat(self: Client, chat: PeerRef) -> None:
        peer = await self.resolver.resolve(chat)
        if peer["_"] == "inputPeerChannel":
            await self.invoke(
                "channels.leaveChannel",
                {"channel": {"_": "inputChannel", "channel_id": peer["channel_id"], "access_hash": peer["access_hash"]}},
            )
        elif peer["_"] == "inputPeerChat":
            await self.invoke(
                "messages.deleteChatUser", {"chat_id": peer["chat_id"], "user_id": {"_": "inputUserSelf"}}
            )
        else:
            raise PeerNotFound("only groups and channels can be left")

    async def get_chat_members(self: Client, chat: PeerRef, *, limit: int = 200, query: str = "") -> list[User]:
        """Members of a channel / supergroup (the server caps this for large chats)."""
        channel = await self.resolver.resolve_channel(chat)
        members: list[User] = []
        offset = 0
        flt: dict[str, Any] = {"_": "channelParticipantsSearch", "q": query} if query else {"_": "channelParticipantsRecent"}
        while len(members) < limit:
            result = await self.invoke(
                "channels.getParticipants",
                {"channel": channel, "filter": flt, "offset": offset, "limit": min(200, limit - len(members)), "hash": 0},
            )
            users = [User.from_raw(u) for u in result.get("users", []) if u.get("_") == "user"]
            if not result.get("participants"):
                break
            members.extend(users)
            offset += len(result["participants"])
        return members[:limit]
