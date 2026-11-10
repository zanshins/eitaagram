"""Update polling.

Eitaa's HTTP transport has no push channel, so updates are fetched by polling
``updates.getDifference`` (and ``updates.getChannelDifference`` for channels).
The cursor lives in the session, so a restarted process resumes where it
stopped (``catch_up=True``) or starts from "now" (default).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from .errors import FloodWait, PeerNotFound, RPCError, SessionExpired, TLError, TransportError
from .types import DeletedMessages, Entities, Message
from .utils import peer_to_chat_id

if TYPE_CHECKING:  # pragma: no cover
    from .client import Client

log = logging.getLogger("eitaagram.updates")

_NEW = {"updateNewMessage", "updateNewChannelMessage"}
_EDIT = {"updateEditMessage", "updateEditChannelMessage"}


class UpdateManager:
    def __init__(
        self,
        client: Client,
        *,
        poll_interval: float = 2.0,
        max_backoff: float = 60.0,
        catch_up: bool = False,
        dedup_size: int = 4096,
    ) -> None:
        self.client = client
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff
        self.catch_up = catch_up
        self._task: asyncio.Task[None] | None = None
        self._seen: OrderedDict[tuple[Any, ...], None] = OrderedDict()
        self._dedup_size = dedup_size
        self.last_error: BaseException | None = None

    # -------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name=f"eitaagram-updates-{self.client.name}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @property
    def _state(self) -> dict[str, Any]:
        return self.client.session.data.state

    # ------------------------------------------------------------------- loop
    async def _run(self) -> None:
        backoff = 1.0
        await asyncio.sleep(random.uniform(0, min(1.0, self.poll_interval)))  # de-synchronize accounts
        if "pts" not in self._state or not self.catch_up:
            await self._guarded(self._sync_state)
        while True:
            more = False
            try:
                more = await self._poll_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except SessionExpired as exc:
                self.last_error = exc
                log.error("[%s] session is no longer valid; update loop stopped", self.client.name)
                await self.client._on_unauthorized(exc)
                return
            except FloodWait as exc:
                self.last_error = exc
                await asyncio.sleep(exc.wait + 0.5)
            except (TransportError, RPCError) as exc:
                self.last_error = exc
                log.warning("[%s] update poll failed: %s (retry in %.0fs)", self.client.name, exc, backoff)
                await asyncio.sleep(backoff + random.uniform(0, 1))
                backoff = min(self.max_backoff, backoff * 2)
            except TLError as exc:
                # An unmodelled constructor: positional decoding is impossible, so skip to
                # the server's current state instead of looping on the same bad batch.
                self.last_error = exc
                log.error("[%s] undecodable update batch (%s); resyncing, some updates are skipped", self.client.name, exc)
                await self._guarded(self._sync_state)
            except Exception as exc:  # defensive: the loop must survive handler/parsing bugs
                self.last_error = exc
                log.exception("[%s] unexpected error in update loop", self.client.name)
                await asyncio.sleep(backoff)
                backoff = min(self.max_backoff, backoff * 2)
            if not more:
                await asyncio.sleep(self.poll_interval)

    async def _guarded(self, func: Any) -> None:
        try:
            await func()
        except asyncio.CancelledError:
            raise
        except (SessionExpired, FloodWait, TransportError, RPCError, TLError) as exc:
            self.last_error = exc
            log.warning("[%s] %s failed: %s", self.client.name, getattr(func, "__name__", "call"), exc)

    async def _sync_state(self) -> None:
        state = await self.client.invoke("updates.getState")
        self._apply_state(state)

    def _apply_state(self, state: dict[str, Any]) -> None:
        for key in ("pts", "qts", "date", "seq"):
            if key in state:
                self._state[key] = int(state[key])
        self.client.session.mark_dirty()

    async def _poll_once(self) -> bool:
        """Fetch one difference batch; return ``True`` when more is pending."""
        state = self._state
        if "pts" not in state:
            await self._sync_state()
            return False
        diff = await self.client.invoke(
            "updates.getDifference",
            {"pts": state["pts"], "date": state.get("date", 0), "qts": state.get("qts", 0)},
        )
        kind = diff.get("_")
        if kind == "updates.differenceEmpty":
            state["date"] = int(diff.get("date", state.get("date", 0)))
            state["seq"] = int(diff.get("seq", state.get("seq", 0)))
            return False
        if kind in ("updates.difference", "updates.differenceSlice"):
            await self._process_difference(diff)
            self._apply_state(diff["state"] if kind == "updates.difference" else diff["intermediate_state"])
            return kind == "updates.differenceSlice"
        if kind == "updates.differenceTooLong":
            log.warning("[%s] difference too long; skipping the gap", self.client.name)
            await self._sync_state()
            return False
        raise TLError(f"unexpected getDifference result: {kind}")

    # ------------------------------------------------------------- processing
    async def _process_difference(self, diff: dict[str, Any]) -> None:
        self.client._ingest(diff)
        entities = Entities(diff.get("users"), diff.get("chats"))
        for raw in diff.get("new_messages", []):
            self._emit_message(raw, entities, edited=False)
        for update in diff.get("other_updates", []):
            await self._handle_update(update, entities)

    async def _handle_update(self, update: dict[str, Any], entities: Entities) -> None:
        name = update.get("_", "")
        self.client.dispatcher.dispatch("raw", update)
        if name in _NEW or name in _EDIT:
            self._emit_message(update["message"], entities, edited=name in _EDIT)
            if name.endswith("ChannelMessage"):
                self._bump_channel_pts(self._channel_of(update["message"]), update.get("pts"))
        elif name == "updateDeleteMessages":
            self.client.dispatcher.dispatch("deleted_messages", DeletedMessages(list(update.get("messages", []))))
        elif name == "updateDeleteChannelMessages":
            chat_id = peer_to_chat_id("channel", int(update["channel_id"]))
            self.client.dispatcher.dispatch("deleted_messages", DeletedMessages(list(update.get("messages", [])), chat_id))
            self._bump_channel_pts(int(update["channel_id"]), update.get("pts"))
        elif name == "updateChannelTooLong":
            await self._fetch_channel_difference(int(update["channel_id"]), update.get("pts"))

    @staticmethod
    def _channel_of(raw: dict[str, Any]) -> int | None:
        peer = raw.get("peer_id") or {}
        return int(peer["channel_id"]) if peer.get("_") == "peerChannel" else None

    def _bump_channel_pts(self, channel_id: int | None, pts: Any) -> None:
        if channel_id is None or not isinstance(pts, int):
            return
        channels: dict[str, int] = self._state.setdefault("channels", {})
        if pts > channels.get(str(channel_id), 0):
            channels[str(channel_id)] = pts
            self.client.session.mark_dirty()

    def _emit_message(self, raw: dict[str, Any], entities: Entities, *, edited: bool) -> None:
        message = Message.from_raw(raw, entities, self.client)
        if message is None:
            return
        key = (message.chat.id, message.id, message.edit_date if edited else None)
        if key in self._seen:
            return
        self._seen[key] = None
        while len(self._seen) > self._dedup_size:
            self._seen.popitem(last=False)
        self.client.dispatcher.dispatch("edited_message" if edited else "message", message)

    async def _fetch_channel_difference(self, channel_id: int, server_pts: Any) -> None:
        channels: dict[str, int] = self._state.setdefault("channels", {})
        local = channels.get(str(channel_id))
        if local is None:
            if not isinstance(server_pts, int):
                return
            local = max(1, server_pts - 1)  # unknown history: best effort, fetch the latest message only
        try:
            channel = await self.client.resolver.resolve_channel(peer_to_chat_id("channel", channel_id))
        except PeerNotFound:
            log.debug("[%s] channel %s not cached; cannot fetch its difference", self.client.name, channel_id)
            return
        for _ in range(20):  # bounded: never spin forever on a busy channel
            diff = await self.client.invoke(
                "updates.getChannelDifference",
                {"channel": channel, "filter": {"_": "channelMessagesFilterEmpty"}, "pts": local, "limit": 100},
            )
            kind = diff.get("_")
            self.client._ingest(diff)
            entities = Entities(diff.get("users"), diff.get("chats"))
            if kind == "updates.channelDifferenceEmpty":
                local = int(diff["pts"])
                break
            if kind == "updates.channelDifference":
                for raw in diff.get("new_messages", []):
                    self._emit_message(raw, entities, edited=False)
                for update in diff.get("other_updates", []):
                    await self._handle_update(update, entities)
                local = int(diff["pts"])
                if diff.get("final"):
                    break
            elif kind == "updates.channelDifferenceTooLong":
                for raw in diff.get("messages", []):
                    self._emit_message(raw, entities, edited=False)
                local = int((diff.get("dialog") or {}).get("pts", local))
                break
            else:
                raise TLError(f"unexpected getChannelDifference result: {kind}")
        channels[str(channel_id)] = local
        self.client.session.mark_dirty()
