"""Sending, editing, deleting, forwarding and reading messages."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from ..errors import TLError
from ..parsing import parse_html
from ..peers import PeerRef
from ..types import Chat, Entities, Message
from ..utils import now, peer_to_chat_id, random_id

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client

_PAGE = 100


def _chat_for_input(client: Client, peer: dict[str, Any]) -> Chat:
    kind = peer.get("_", "")
    if kind == "inputPeerSelf":
        ident = client.session.data.user_id or 0
        return Chat(id=ident, kind="private", raw_id=ident)
    if kind == "inputPeerUser":
        return Chat(id=int(peer["user_id"]), kind="private", raw_id=int(peer["user_id"]), access_hash=peer.get("access_hash"))
    if kind == "inputPeerChat":
        return Chat(id=peer_to_chat_id("chat", int(peer["chat_id"])), kind="group", raw_id=int(peer["chat_id"]))
    ident = int(peer["channel_id"])
    return Chat(id=peer_to_chat_id("channel", ident), kind="channel", raw_id=ident, access_hash=peer.get("access_hash"))


def messages_from_updates(result: Any, client: Client | None) -> list[Message]:
    """Extract the (new) messages contained in an ``Updates`` result."""
    if not isinstance(result, dict):
        return []
    kind = result.get("_")
    if kind == "updateShort":
        updates, entities = [result.get("update") or {}], Entities()
    elif kind in ("updates", "updatesCombined"):
        updates = result.get("updates", [])
        entities = Entities(result.get("users"), result.get("chats"))
    else:
        return []
    found: list[Message] = []
    for update in updates:
        if update.get("_") in ("updateNewMessage", "updateNewChannelMessage"):
            message = Message.from_raw(update["message"], entities, client)
            if message is not None:
                found.append(message)
    return found


def _parse(text: str, parse_mode: str | None, entities: list[dict[str, Any]] | None) -> tuple[str, list[dict[str, Any]] | None]:
    if parse_mode is None:
        return text, entities
    if parse_mode.lower() != "html":
        raise ValueError("parse_mode must be None or 'html'")
    clean, found = parse_html(text)
    return clean, (found or None)


class MessageMethods:
    async def send_message(
        self: Client,
        chat: PeerRef,
        text: str,
        *,
        reply_to: int | None = None,
        parse_mode: str | None = None,
        entities: list[dict[str, Any]] | None = None,
        silent: bool = False,
        disable_web_page_preview: bool = False,
        schedule_date: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> Message:
        peer = await self.resolver.resolve(chat)
        body, ents = _parse(text, parse_mode, entities)
        params: dict[str, Any] = {
            "peer": peer,
            "message": body,
            "random_id": random_id(),
            "silent": silent,
            "no_webpage": disable_web_page_preview,
            "reply_to_msg_id": reply_to,
            "entities": ents,
            "schedule_date": schedule_date,
            "reply_markup": reply_markup,
        }
        result = await self.invoke("messages.sendMessage", {k: v for k, v in params.items() if v is not None})
        return self._sent_message(result, peer, body)

    def _sent_message(self: Client, result: Any, peer: dict[str, Any], text: str) -> Message:
        found = messages_from_updates(result, self)
        if found:
            return found[0]
        if isinstance(result, dict) and result.get("_") == "updateShortSentMessage":
            return Message(
                id=int(result["id"]),
                chat=_chat_for_input(self, peer),
                date=int(result.get("date", now())),
                text=text,
                outgoing=True,
                sender_id=self.session.data.user_id,
                raw=result,
                client=self,
            )
        if isinstance(result, dict) and result.get("_") in ("updates", "updatesCombined"):
            for update in result.get("updates", []):
                if update.get("_") == "updateMessageID":
                    return Message(
                        id=int(update["id"]),
                        chat=_chat_for_input(self, peer),
                        date=now(),
                        text=text,
                        outgoing=True,
                        sender_id=self.session.data.user_id,
                        raw=result,
                        client=self,
                    )
        raise TLError(f"could not find the sent message in the server reply ({result.get('_') if isinstance(result, dict) else type(result).__name__})")

    async def edit_message_text(
        self: Client,
        chat: PeerRef,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        entities: list[dict[str, Any]] | None = None,
        disable_web_page_preview: bool = False,
        reply_markup: dict[str, Any] | None = None,
    ) -> Message:
        peer = await self.resolver.resolve(chat)
        body, ents = _parse(text, parse_mode, entities)
        params: dict[str, Any] = {
            "peer": peer,
            "id": message_id,
            "message": body,
            "no_webpage": disable_web_page_preview,
            "entities": ents,
            "reply_markup": reply_markup,
        }
        result = await self.invoke("messages.editMessage", {k: v for k, v in params.items() if v is not None})
        for message in messages_from_updates(result, self):
            if message.id == message_id:
                return message
        edited = Entities(result.get("users"), result.get("chats")) if isinstance(result, dict) else Entities()
        for update in result.get("updates", []) if isinstance(result, dict) else []:
            if update.get("_") in ("updateEditMessage", "updateEditChannelMessage"):
                message = Message.from_raw(update["message"], edited, self)
                if message is not None:
                    return message
        return Message(id=message_id, chat=_chat_for_input(self, peer), date=now(), text=body, outgoing=True, client=self)

    async def delete_messages(self: Client, chat: PeerRef, message_ids: Sequence[int], *, revoke: bool = True) -> int:
        """Delete messages; returns how many the server reports as affected."""
        ids = list(message_ids)
        if not ids:
            return 0
        peer = await self.resolver.resolve(chat)
        if peer["_"] == "inputPeerChannel":
            channel = {"_": "inputChannel", "channel_id": peer["channel_id"], "access_hash": peer["access_hash"]}
            result = await self.invoke("channels.deleteMessages", {"channel": channel, "id": ids})
        else:
            result = await self.invoke("messages.deleteMessages", {"id": ids, "revoke": revoke})
        return int(result.get("pts_count", len(ids)))

    async def forward_messages(
        self: Client,
        chat: PeerRef,
        from_chat: PeerRef,
        message_ids: Sequence[int],
        *,
        silent: bool = False,
        drop_author: bool = False,
        schedule_date: int | None = None,
    ) -> list[Message]:
        ids = list(message_ids)
        if not ids:
            return []
        to_peer = await self.resolver.resolve(chat)
        from_peer = await self.resolver.resolve(from_chat)
        params: dict[str, Any] = {
            "from_peer": from_peer,
            "id": ids,
            "random_id": [random_id() for _ in ids],
            "to_peer": to_peer,
            "silent": silent,
            "drop_author": drop_author,
            "schedule_date": schedule_date,
        }
        result = await self.invoke("messages.forwardMessages", {k: v for k, v in params.items() if v is not None})
        return messages_from_updates(result, self)

    async def get_messages(self: Client, chat: PeerRef, message_ids: Sequence[int]) -> list[Message]:
        ids = [{"_": "inputMessageID", "id": i} for i in message_ids]
        if not ids:
            return []
        peer = await self.resolver.resolve(chat)
        if peer["_"] == "inputPeerChannel":
            channel = {"_": "inputChannel", "channel_id": peer["channel_id"], "access_hash": peer["access_hash"]}
            result = await self.invoke("channels.getMessages", {"channel": channel, "id": ids})
        else:
            result = await self.invoke("messages.getMessages", {"id": ids})
        return self._messages_from(result)

    def _messages_from(self: Client, result: Any) -> list[Message]:
        if not isinstance(result, dict) or "messages" not in result:
            return []  # e.g. messages.messagesNotModified
        entities = Entities(result.get("users"), result.get("chats"))
        out: list[Message] = []
        for raw in result["messages"]:
            message = Message.from_raw(raw, entities, self)
            if message is not None:
                out.append(message)
        return out

    async def get_history(
        self: Client, chat: PeerRef, *, limit: int = 0, offset_id: int = 0, min_id: int = 0, max_id: int = 0
    ) -> AsyncIterator[Message]:
        """Iterate messages newest -> oldest. ``limit=0`` means "everything"."""
        peer = await self.resolver.resolve(chat)
        yielded = 0
        while True:
            page = _PAGE if not limit else min(_PAGE, limit - yielded)
            result = await self.invoke(
                "messages.getHistory",
                {
                    "peer": peer,
                    "offset_id": offset_id,
                    "offset_date": 0,
                    "add_offset": 0,
                    "limit": page,
                    "max_id": max_id,
                    "min_id": min_id,
                    "hash": 0,
                },
            )
            batch = self._messages_from(result)
            if not batch:
                return
            for message in batch:
                yield message
                yielded += 1
                if limit and yielded >= limit:
                    return
            offset_id = batch[-1].id

    async def search_messages(
        self: Client, chat: PeerRef, query: str, *, limit: int = 100, offset_id: int = 0
    ) -> AsyncIterator[Message]:
        peer = await self.resolver.resolve(chat)
        yielded = 0
        while not limit or yielded < limit:
            result = await self.invoke(
                "messages.search",
                {
                    "peer": peer,
                    "q": query,
                    "filter": {"_": "inputMessagesFilterEmpty"},
                    "min_date": 0,
                    "max_date": 0,
                    "offset_id": offset_id,
                    "add_offset": 0,
                    "limit": _PAGE if not limit else min(_PAGE, limit - yielded),
                    "max_id": 0,
                    "min_id": 0,
                    "hash": 0,
                },
            )
            batch = self._messages_from(result)
            if not batch:
                return
            for message in batch:
                yield message
                yielded += 1
                if limit and yielded >= limit:
                    return
            offset_id = batch[-1].id

    async def read_history(self: Client, chat: PeerRef, *, max_id: int = 0) -> bool:
        peer = await self.resolver.resolve(chat)
        if peer["_"] == "inputPeerChannel":
            channel = {"_": "inputChannel", "channel_id": peer["channel_id"], "access_hash": peer["access_hash"]}
            return bool(await self.invoke("channels.readHistory", {"channel": channel, "max_id": max_id}))
        await self.invoke("messages.readHistory", {"peer": peer, "max_id": max_id})
        return True

    async def send_chat_action(self: Client, chat: PeerRef, action: str = "typing") -> bool:
        """Typing indicators need the realtime socket, which this transport lacks (always ``False``)."""
        return False


