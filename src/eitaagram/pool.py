"""Multi-account management.

:class:`AccountPool` owns many :class:`Client` objects and gives you what a
tabchi / self-bot farm actually needs: one place to log accounts in, a health
state per account, shared rate limiting, pool-wide event handlers, and a
fan-out helper that respects each account's FloodWait.

It deliberately provides no stealth or evasion features: every account keeps a
stable identity (its own ``imei`` / profile) and the server's waits are honoured.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import inspect
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from .client import Client
from .dispatcher import (
    DeletedMessagesHandler,
    EditedMessageHandler,
    Handler,
    MessageHandler,
    RawUpdateHandler,
)
from .errors import (
    AccountNotFound,
    AccountUnavailable,
    EitaaError,
    FloodWait,
    PoolError,
    SessionExpired,
    SessionRevoked,
    TransportError,
)
from .filters import Filter
from .profile import ClientProfile
from .ratelimit import RateLimiter
from .session import Session, SQLiteSession
from .transport import HttpTransport

log = logging.getLogger("eitaagram.pool")
T = TypeVar("T")

SessionFactory = Callable[[str], Session]
CodeCallback = Callable[[str, Any], str | Awaitable[str]]  # (account_name, SentCode) -> code


class AccountState(enum.Enum):
    NEW = "new"  # registered, not started
    STARTING = "starting"
    READY = "ready"
    FLOOD = "flood"  # recently told to wait; becomes READY again by itself
    UNAUTHORIZED = "unauthorized"  # token dead and refresh failed -> needs login
    BANNED = "banned"  # revoked / deactivated
    ERROR = "error"  # transport or unexpected failure while starting
    STOPPED = "stopped"


@dataclass(slots=True)
class Account:
    name: str
    client: Client
    state: AccountState = AccountState.NEW
    last_error: str | None = None
    flood_until: float = 0.0  # monotonic
    in_flight: int = 0
    tags: set[str] = field(default_factory=set)

    @property
    def usable(self) -> bool:
        if self.state is AccountState.FLOOD and time.monotonic() >= self.flood_until:
            self.state = AccountState.READY
        return self.state is AccountState.READY

    @property
    def flood_remaining(self) -> float:
        return max(0.0, self.flood_until - time.monotonic())


class AccountPool:
    """A named collection of accounts sharing configuration and limits.

    Args:
        sessions: where accounts are persisted -- a path to one SQLite file
            (many accounts per file, recommended), or a ``name -> Session`` factory.
        profile: shared :class:`ClientProfile`.
        proxies: optional ``{account_name: proxy_url}``; accounts without an entry
            share one HTTP connection pool.
        global_rate: calls/second shared by *all* accounts (``None`` = unlimited).
        account_rate: calls/second per account.
        max_concurrent_starts: login/validation parallelism in :meth:`start`.
    """

    def __init__(
        self,
        sessions: str | Path | SessionFactory = "accounts.db",
        *,
        profile: ClientProfile | None = None,
        proxies: dict[str, str] | None = None,
        global_rate: float | None = 20.0,
        global_burst: int = 20,
        account_rate: float | None = 5.0,
        account_burst: int = 5,
        poll_interval: float = 3.0,
        max_concurrent_starts: int = 5,
        client_options: dict[str, Any] | None = None,
    ) -> None:
        if callable(sessions):
            self._factory: SessionFactory = sessions
            self._db_path: Path | None = None
        else:
            self._db_path = Path(sessions).expanduser()
            path = self._db_path
            self._factory = lambda name: SQLiteSession(path, name)
        self.profile = profile or ClientProfile()
        self.proxies = dict(proxies or {})
        self._global_limiter = RateLimiter(global_rate, global_burst) if global_rate else None
        self._account_rate = account_rate
        self._account_burst = account_burst
        self._poll_interval = poll_interval
        self._start_sem = asyncio.Semaphore(max(1, max_concurrent_starts))
        self._options = dict(client_options or {})
        self._shared_transport: HttpTransport | None = None
        self.accounts: dict[str, Account] = {}
        self._handlers: list[tuple[str, Callable[..., Any], Filter | None, int]] = []
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._rr = 0
        self._stop_event: asyncio.Event | None = None
        self._started = False

    # ---------------------------------------------------------------- registry
    def __len__(self) -> int:
        return len(self.accounts)

    def __contains__(self, name: object) -> bool:
        return name in self.accounts

    def __getitem__(self, name: str) -> Client:
        return self.get(name).client

    def get(self, name: str) -> Account:
        try:
            return self.accounts[name]
        except KeyError:
            raise AccountNotFound(f"no account named {name!r}") from None

    def stored_names(self) -> list[str]:
        """Account names present in the backing SQLite file."""
        return SQLiteSession.list_names(self._db_path) if self._db_path else []

    def _transport_for(self, name: str) -> HttpTransport:
        proxy = self.proxies.get(name)
        if proxy:
            return HttpTransport(self.profile, proxy=proxy)  # owned by that Client
        if self._shared_transport is None:
            self._shared_transport = HttpTransport(self.profile)
        return self._shared_transport

    def add(
        self,
        name: str,
        *,
        phone: str | None = None,
        session: Session | None = None,
        tags: Iterable[str] = (),
        **client_kwargs: Any,
    ) -> Account:
        """Register an account (does not start it)."""
        if name in self.accounts:
            raise PoolError(f"account {name!r} already exists")
        session = session or self._factory(name)
        limiters = [self._global_limiter] if self._global_limiter else []
        options: dict[str, Any] = {
            "profile": self.profile,
            "rate_limit": self._account_rate,
            "burst": self._account_burst,
            "poll_interval": self._poll_interval,
            "extra_limiters": limiters,
            **self._options,
            **client_kwargs,
        }
        transport = options.pop("transport", None) or self._transport_for(name)
        client = Client(session, phone=phone, transport=transport, **options)
        # a shared transport is closed by the pool, a per-proxy one by its client
        client._owns_transport = transport is not self._shared_transport
        account = Account(name=name, client=client, tags=set(tags))
        self._wire(account)
        self.accounts[name] = account
        for kind, callback, flt, group in self._handlers:
            self._attach(client, kind, callback, flt, group)
        return account

    def load_stored(self, **client_kwargs: Any) -> list[Account]:
        """Register every account found in the SQLite file that is not registered yet."""
        added = []
        for name in self.stored_names():
            if name not in self.accounts:
                added.append(self.add(name, **client_kwargs))
        return added

    async def remove(self, name: str, *, logout: bool = False) -> None:
        account = self.get(name)
        if logout:
            with contextlib.suppress(EitaaError):
                await account.client.log_out()
        await account.client.stop()
        del self.accounts[name]

    def _wire(self, account: Account) -> None:
        client = account.client

        def on_flood(_c: Client, method: str, wait: float) -> None:
            account.flood_until = max(account.flood_until, time.monotonic() + wait)
            if account.state is AccountState.READY:
                account.state = AccountState.FLOOD
            self._notify("flood", account, method, wait)

        def on_lost(_c: Client, exc: BaseException) -> None:
            account.state = AccountState.BANNED if isinstance(exc, SessionRevoked) else AccountState.UNAUTHORIZED
            account.last_error = str(exc)
            self._notify("unauthorized", account, exc)

        def on_refreshed(_c: Client, expires: Any) -> None:
            self._notify("token_refreshed", account, expires)

        client.add_listener("flood_wait", on_flood)
        client.add_listener("unauthorized", on_lost)
        client.add_listener("token_refreshed", on_refreshed)

    # ------------------------------------------------------------------ events
    def add_listener(self, event: str, callback: Callable[..., Any]) -> None:
        """Pool events: ``flood`` / ``unauthorized`` / ``token_refreshed`` ->
        ``callback(account, ...)``; ``state`` -> ``callback(account, old, new)``."""
        self._listeners.setdefault(event, []).append(callback)

    def _notify(self, event: str, *args: Any) -> None:
        for callback in self._listeners.get(event, ()):
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    asyncio.ensure_future(result)
            except Exception:
                log.exception("pool listener for %r failed", event)

    def _set_state(self, account: Account, state: AccountState, error: str | None = None) -> None:
        old = account.state
        account.state = state
        account.last_error = error
        if old is not state:
            self._notify("state", account, old, state)

    # ---------------------------------------------------------------- handlers
    def _attach(self, client: Client, kind: str, callback: Callable[..., Any], flt: Filter | None, group: int) -> None:
        handler: Handler
        if kind == "message":
            handler = MessageHandler(callback, flt)
        elif kind == "edited_message":
            handler = EditedMessageHandler(callback, flt)
        elif kind == "deleted_messages":
            handler = DeletedMessagesHandler(callback)
        else:
            handler = RawUpdateHandler(callback)
        client.add_handler(handler, group)

    def _register(self, kind: str, callback: Callable[..., Any], flt: Filter | None, group: int) -> None:
        self._handlers.append((kind, callback, flt, group))
        for account in self.accounts.values():
            self._attach(account.client, kind, callback, flt, group)

    def on_message(self, filters: Filter | None = None, group: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a handler on *every* account (present and future).

        The callback receives ``(client, message)``; ``client.name`` identifies the account.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._register("message", func, filters, group)
            return func

        return decorator

    def on_edited_message(self, filters: Filter | None = None, group: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._register("edited_message", func, filters, group)
            return func

        return decorator

    # --------------------------------------------------------------- lifecycle
    async def start(
        self,
        names: Iterable[str] | None = None,
        *,
        code_callback: CodeCallback | None = None,
        interactive: bool = False,
    ) -> dict[str, AccountState]:
        """Start accounts concurrently (bounded) and return their resulting states.

        Accounts that already hold a valid session just validate and begin polling.
        Accounts without a session are logged in only if ``code_callback`` is given
        (or ``interactive=True``, which asks on stdin); otherwise they end up
        ``UNAUTHORIZED`` and are skipped -- one bad account never blocks the rest.
        """
        targets = [self.get(n) for n in names] if names is not None else list(self.accounts.values())
        await asyncio.gather(*(self._start_one(a, code_callback, interactive) for a in targets))
        self._started = True
        return {a.name: a.state for a in targets}

    async def _start_one(self, account: Account, code_callback: CodeCallback | None, interactive: bool) -> None:
        async with self._start_sem:
            client = account.client
            self._set_state(account, AccountState.STARTING)
            try:
                await client.connect()
                needs_login = not client.session.authorized
                if needs_login and not (code_callback or interactive):
                    self._set_state(account, AccountState.UNAUTHORIZED, "no session; login required")
                    return
                callback = None
                if needs_login and code_callback is not None:
                    cb = code_callback

                    async def callback(sent: Any, _name: str = account.name, _cb: CodeCallback = cb) -> str:
                        result = _cb(_name, sent)
                        if inspect.isawaitable(result):
                            result = await result
                        return str(result)

                if needs_login and not client.phone:
                    self._set_state(account, AccountState.UNAUTHORIZED, "phone number missing")
                    return
                await client.start(code_callback=callback)
                self._set_state(account, AccountState.READY)
            except SessionRevoked as exc:
                self._set_state(account, AccountState.BANNED, str(exc))
            except SessionExpired as exc:
                self._set_state(account, AccountState.UNAUTHORIZED, str(exc))
            except FloodWait as exc:
                account.flood_until = time.monotonic() + exc.wait
                self._set_state(account, AccountState.FLOOD, str(exc))
            except (TransportError, EitaaError) as exc:
                self._set_state(account, AccountState.ERROR, str(exc))
            except Exception as exc:  # keep the batch alive no matter what one account does
                log.exception("[%s] failed to start", account.name)
                self._set_state(account, AccountState.ERROR, repr(exc))

    async def stop(self) -> None:
        await asyncio.gather(*(a.client.stop() for a in self.accounts.values()), return_exceptions=True)
        for account in self.accounts.values():
            account.state = AccountState.STOPPED
        if self._shared_transport is not None:
            await self._shared_transport.close()
            self._shared_transport = None
        self._started = False

    async def __aenter__(self) -> AccountPool:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def idle(self) -> None:
        import signal

        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._stop_event.set)
        await self._stop_event.wait()

    async def run(self, **start_kwargs: Any) -> None:
        await self.start(**start_kwargs)
        try:
            await self.idle()
        finally:
            await self.stop()

    # ------------------------------------------------------------------- query
    def ready(self, *, tag: str | None = None) -> list[Account]:
        return [a for a in self.accounts.values() if a.usable and (tag is None or tag in a.tags)]

    def status(self) -> dict[str, dict[str, Any]]:
        """Snapshot for dashboards / logs (never includes tokens)."""
        return {
            name: {
                "state": a.state.value,
                "phone": a.client.session.data.phone,
                "user_id": a.client.session.data.user_id,
                "flood_remaining": round(a.flood_remaining),
                "in_flight": a.in_flight,
                "error": a.last_error,
                "stats": dict(a.client.invoker.stats) if a.client.invoker else {},
            }
            for name, a in self.accounts.items()
        }

    def pick(self, strategy: str = "round_robin", *, tag: str | None = None) -> Account:
        """Choose a usable account: ``round_robin`` | ``least_busy`` | ``random``."""
        candidates = self.ready(tag=tag)
        if not candidates:
            raise AccountUnavailable("no account is currently ready")
        if strategy == "random":
            return random.choice(candidates)
        if strategy == "least_busy":
            return min(candidates, key=lambda a: a.in_flight)
        if strategy != "round_robin":
            raise ValueError(f"unknown strategy {strategy!r}")
        self._rr = (self._rr + 1) % len(candidates)
        return candidates[self._rr]

    # ------------------------------------------------------------------ fan-out
    async def run_on(self, name: str, func: Callable[[Client], Awaitable[T]]) -> T:
        account = self.get(name)
        account.in_flight += 1
        try:
            return await func(account.client)
        finally:
            account.in_flight -= 1

    async def map(
        self,
        func: Callable[[Client], Awaitable[T]],
        *,
        accounts: Sequence[str] | None = None,
        tag: str | None = None,
        concurrency: int = 5,
        skip_unusable: bool = True,
    ) -> dict[str, T | BaseException]:
        """Run ``func(client)`` on several accounts with bounded concurrency.

        One failing account never aborts the others: its exception is returned in
        the result dict.  Accounts that are rate-limited by the server are skipped
        (state ``FLOOD``) instead of being hammered.
        """
        if accounts is not None:
            chosen = [self.get(n) for n in accounts]
        else:
            chosen = list(self.accounts.values()) if not skip_unusable else self.ready(tag=tag)
            if tag is not None and not skip_unusable:
                chosen = [a for a in chosen if tag in a.tags]
        if skip_unusable:
            chosen = [a for a in chosen if a.usable]
        sem = asyncio.Semaphore(max(1, concurrency))
        results: dict[str, T | BaseException] = {}

        async def one(account: Account) -> None:
            async with sem:
                try:
                    results[account.name] = await self.run_on(account.name, func)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    results[account.name] = exc

        await asyncio.gather(*(one(a) for a in chosen))
        return results

    # ------------------------------------------------------------------ export
    def export_sessions(self) -> dict[str, str]:
        """``{name: eg1:... string}`` for backups / migration (contains tokens!)."""
        return {n: a.client.export_session_string() for n, a in self.accounts.items() if a.client.session.authorized}
