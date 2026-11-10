"""The high-level :class:`Client` (one Eitaa account)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from .dispatcher import (
    DeletedMessagesHandler,
    Dispatcher,
    EditedMessageHandler,
    Handler,
    MessageHandler,
    RawUpdateHandler,
)
from .errors import SessionExpired
from .filters import Filter
from .methods import AccountMethods, AuthMethods, ChatMethods, FileMethods, MessageMethods
from .peers import PeerResolver
from .profile import ClientProfile
from .ratelimit import RateLimiter
from .rpc import Invoker
from .session import MemorySession, Session, SQLiteSession, StringSession
from .tl import TLCodec
from .transport import HttpTransport, Transport
from .types import User

log = logging.getLogger("eitaagram.client")

Listener = Callable[..., Awaitable[None] | None]
_CODEC: TLCodec | None = None


def _shared_codec() -> TLCodec:
    """The schema is large; parse it once per process and share it between accounts."""
    global _CODEC
    if _CODEC is None:
        _CODEC = TLCodec()
    return _CODEC


class Client(AuthMethods, MessageMethods, ChatMethods, AccountMethods, FileMethods):
    """One Eitaa account.

    ``session`` may be

    * a name -> SQLite file ``<workdir>/<name>.session`` (Telethon-style),
    * a :class:`~eitaagram.session.Session` instance (``StringSession``, ``FileSession``...),
    * a session *string* produced by ``StringSession.export()``.

    Example::

        async with Client("alice", phone="+989121234567") as app:
            await app.send_message("me", "hello")
    """

    def __init__(
        self,
        session: str | Session = "default",
        *,
        phone: str | None = None,
        profile: ClientProfile | None = None,
        proxy: str | None = None,
        transport: Transport | None = None,
        workdir: str | os.PathLike[str] = ".",
        in_memory: bool = False,
        workers: int = 4,
        rate_limit: float | None = 5.0,
        burst: int = 5,
        extra_limiters: Sequence[RateLimiter] = (),
        flood_sleep_threshold: float = 60.0,
        max_retries: int = 3,
        auto_refresh_token: bool = True,
        poll_interval: float = 2.0,
        catch_up: bool = False,
        no_updates: bool = False,
        session_flush_interval: float = 30.0,
        codec: TLCodec | None = None,
    ) -> None:
        self.session = self._make_session(session, workdir, in_memory)
        self.phone = phone
        self.profile = profile or ClientProfile()
        self.proxy = proxy
        self.no_updates = no_updates
        self._transport = transport
        self._owns_transport = transport is None
        self._codec = codec or _shared_codec()
        self._limiters: list[RateLimiter] = list(extra_limiters)
        if rate_limit:
            self._limiters.append(RateLimiter(rate_limit, burst))
        self._flood_sleep_threshold = flood_sleep_threshold
        self._max_retries = max_retries
        self._auto_refresh = auto_refresh_token
        self._flush_interval = session_flush_interval

        self.invoker: Invoker | None = None
        self.resolver = PeerResolver(self.session, self.invoke)
        self.dispatcher = Dispatcher(self, workers=workers)
        from .updates import UpdateManager  # local import: avoids a cycle at module load

        self.updates = UpdateManager(self, poll_interval=poll_interval, catch_up=catch_up)

        self._listeners: dict[str, list[Listener]] = {}
        self._me: User | None = None
        self._connected = False
        self._flush_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self.unauthorized = False

    # ----------------------------------------------------------------- setup
    @staticmethod
    def _make_session(session: str | Session, workdir: str | os.PathLike[str], in_memory: bool) -> Session:
        if isinstance(session, Session):
            return session
        if session.startswith("eg1:"):
            return StringSession(session)
        if in_memory:
            return MemorySession(session)
        return SQLiteSession(Path(workdir).expanduser() / f"{session}.session", session)

    @property
    def name(self) -> str:
        return self.session.name

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = HttpTransport(self.profile, proxy=self.proxy)
        return self._transport

    @property
    def me(self) -> User | None:
        return self._me

    @property
    def me_id(self) -> int | None:
        return self.session.data.user_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_authorized(self) -> bool:
        return self.session.authorized and not self.unauthorized

    def export_session_string(self) -> str:
        """Portable ``eg1:...`` string for this account (contains the token!)."""
        if isinstance(self.session, StringSession):
            return self.session.export()
        clone = StringSession(name=self.session.name)
        clone.data = self.session.data
        return clone.export()

    # ------------------------------------------------------------- listeners
    def add_listener(self, event: str, callback: Listener) -> None:
        """Events: ``authorized``, ``token_refreshed``, ``flood_wait``, ``session_lost``,
        ``unauthorized``, ``started``, ``stopped``."""
        self._listeners.setdefault(event, []).append(callback)

    async def _emit(self, event: str, *args: Any) -> None:
        for callback in list(self._listeners.get(event, ())):
            try:
                result = callback(self, *args)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("[%s] listener for %r failed", self.name, event)

    async def _on_unauthorized(self, exc: BaseException) -> None:
        if self.unauthorized:
            return  # already reported (the RPC layer and the update loop can both notice)
        self.unauthorized = True
        await self._emit("unauthorized", exc)

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        """Load the session and prepare the RPC engine (no login, no polling)."""
        if self._connected:
            return
        await self.session.load()
        self.invoker = Invoker(
            codec=self._codec,
            transport=self.transport,
            session=self.session,
            profile=self.profile,
            limiters=self._limiters,
            flood_sleep_threshold=self._flood_sleep_threshold,
            max_retries=self._max_retries,
            auto_refresh=self._auto_refresh,
        )
        self.invoker.on("token_refreshed", lambda expires: self._emit("token_refreshed", expires))
        self.invoker.on("flood_wait", lambda method, wait: self._emit("flood_wait", method, wait))
        self.invoker.on("session_lost", lambda exc: self._on_unauthorized(exc))
        self._connected = True
        self.unauthorized = False
        if self._flush_interval and self._flush_interval > 0:
            self._flush_task = asyncio.create_task(self._flush_loop(), name=f"eitaagram-flush-{self.name}")

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.session.flush()
            except Exception:
                log.exception("[%s] could not persist the session", self.name)

    async def disconnect(self) -> None:
        if not self._connected:
            return
        await self.updates.stop()
        await self.dispatcher.drain()
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        with contextlib.suppress(Exception):
            await self.session.flush()
        if self._owns_transport and self._transport is not None:
            await self._transport.close()
            self._transport = None
        self._connected = False

    async def start(
        self,
        *,
        phone: str | None = None,
        code_callback: Any = None,
        first_name: str | None = None,
        last_name: str = "",
    ) -> Client:
        """Connect, log in if necessary, validate the token and begin polling updates."""
        await self.connect()
        phone = phone or self.phone
        if phone:
            self.phone = phone
        if self.session.authorized:
            try:
                await self.get_me()
            except SessionExpired:
                if not (phone or code_callback):
                    raise
                log.warning("[%s] stored session is dead; logging in again", self.name)
                self.session.data.token = None
                self.unauthorized = False
                await self.authorize(phone, code_callback=code_callback, first_name=first_name, last_name=last_name)
        else:
            await self.authorize(phone, code_callback=code_callback, first_name=first_name, last_name=last_name)
        if self._me is None:
            await self.get_me()
        await self.session.save()
        if not self.no_updates:
            self.updates.start()
        await self._emit("started")
        return self

    async def stop(self) -> None:
        await self.disconnect()
        await self._emit("stopped")

    async def __aenter__(self) -> Client:
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def idle(self) -> None:
        """Block until SIGINT / SIGTERM (or :meth:`request_stop`)."""
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._stop_event.set)
                installed.append(sig)
        try:
            await self._stop_event.wait()
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)

    def request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def run(self, **start_kwargs: Any) -> None:
        """``start()`` + ``idle()`` + ``stop()``."""
        await self.start(**start_kwargs)
        try:
            await self.idle()
        finally:
            await self.stop()

    # ------------------------------------------------------------------ core
    async def invoke(self, method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """Raw TL call: ``await app.invoke("users.getUsers", {"id": [...]})``."""
        if self.invoker is None:
            await self.connect()
        assert self.invoker is not None
        result = await self.invoker.invoke(method, params, **kwargs)
        self._ingest(result)
        return result

    def _ingest(self, obj: Any) -> None:
        """Learn access hashes from any response carrying ``users`` / ``chats``."""
        if isinstance(obj, dict):
            if obj.get("users") or obj.get("chats"):
                self.resolver.ingest(obj.get("users"), obj.get("chats"))
            update = obj.get("update")
            if isinstance(update, dict):
                self._ingest(update)
        elif isinstance(obj, list):
            users = [x for x in obj if isinstance(x, dict) and x.get("_") == "user"]
            if users:
                self.resolver.ingest(users, None)

    async def refresh_token(self) -> bool:
        """Force an ``eitaaRefreshToken`` round-trip."""
        if self.invoker is None:
            await self.connect()
        assert self.invoker is not None
        return await self.invoker.refresh_token()

    # -------------------------------------------------------------- handlers
    def add_handler(self, handler: Handler, group: int = 0) -> Handler:
        return self.dispatcher.add_handler(handler, group)

    def remove_handler(self, handler: Handler, group: int = 0) -> None:
        self.dispatcher.remove_handler(handler, group)

    def on_message(self, filters: Filter | None = None, group: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add_handler(MessageHandler(func, filters), group)
            return func

        return decorator

    def on_edited_message(self, filters: Filter | None = None, group: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add_handler(EditedMessageHandler(func, filters), group)
            return func

        return decorator

    def on_deleted_messages(self, group: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add_handler(DeletedMessagesHandler(func), group)
            return func

        return decorator

    def on_raw_update(self, group: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add_handler(RawUpdateHandler(func), group)
            return func

        return decorator

    def __repr__(self) -> str:
        state = "authorized" if self.is_authorized else "anonymous"
        return f"<Client {self.name!r} {state}>"


