"""The RPC engine: wraps a TL call in Eitaa's envelope and makes it reliable.

Responsibilities (all per account):

* build the ``eitaaObject`` envelope (token / imei / layer / flags) and decode the reply;
* single-flight token refresh: N concurrent calls that hit an expired token
  trigger exactly one ``eitaaRefreshToken`` and then retry once;
* FloodWait handling: short waits are slept through, long waits fail fast, and
  a per-method guard stops the account from hammering a method the server told
  it to rest;
* bounded exponential backoff for transport / 5xx errors;
* optional rate limiting and learned ``INVALID_CONSTRUCTOR`` blacklist.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .errors import (
    FloodWait,
    InvalidConstructor,
    MethodNotSupported,
    NotAuthorized,
    RPCError,
    ServerError,
    SessionExpired,
    SessionRevoked,
    TLError,
    TransportError,
)
from .profile import ClientProfile
from .ratelimit import RateLimiter
from .session import Session
from .tl import TLCodec, TLValue
from .transport import Transport
from .utils import now

log = logging.getLogger("eitaagram.rpc")

# Methods the web transport cannot carry (socket-only); learned at runtime too.
_STATIC_UNSUPPORTED = frozenset({"messages.setTyping"})

EventHook = Callable[..., Awaitable[None] | None]


class Invoker:
    def __init__(
        self,
        *,
        codec: TLCodec,
        transport: Transport,
        session: Session,
        profile: ClientProfile,
        limiters: Sequence[RateLimiter] = (),
        flood_sleep_threshold: float = 60.0,
        max_retries: int = 3,
        auto_refresh: bool = True,
        backoff_base: float = 0.75,
    ) -> None:
        self.codec = codec
        self.transport = transport
        self.session = session
        self.profile = profile
        self.limiters = tuple(limiters)
        self.flood_sleep_threshold = flood_sleep_threshold
        self.max_retries = max_retries
        self.auto_refresh = auto_refresh
        self.backoff_base = backoff_base

        self._refresh_lock = asyncio.Lock()
        self._token_generation = 0
        self._flood_until: dict[str, float] = {}  # method -> monotonic deadline
        self._unsupported: set[str] = set(_STATIC_UNSUPPORTED)
        self._hooks: dict[str, list[EventHook]] = {}
        self.stats = {"calls": 0, "errors": 0, "refreshes": 0, "flood_waits": 0, "retries": 0}

    # ----------------------------------------------------------------- events
    def on(self, event: str, hook: EventHook) -> None:
        """Events: ``token_refreshed``, ``session_lost``, ``flood_wait``."""
        self._hooks.setdefault(event, []).append(hook)

    async def _emit(self, event: str, *args: Any) -> None:
        for hook in self._hooks.get(event, ()):
            try:
                result = hook(*args)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # a broken hook must never break an API call
                log.exception("hook for %r failed", event)

    # ------------------------------------------------------------------- core
    def flood_remaining(self, method: str) -> float:
        return max(0.0, self._flood_until.get(method, 0.0) - time.monotonic())

    async def invoke(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        kind: str = "client",
        authorized: bool = True,
        endpoint: str | None = None,
        retries: int | None = None,
        flood_sleep_threshold: float | None = None,
    ) -> TLValue:
        """Call ``method``; returns decoded TL data (dicts / lists / scalars)."""
        if method in self._unsupported:
            raise MethodNotSupported(f"{method} is not available over the HTTP transport")
        if authorized and not self.session.data.token:
            raise NotAuthorized("this account is not logged in")

        max_retries = self.max_retries if retries is None else retries
        threshold = self.flood_sleep_threshold if flood_sleep_threshold is None else flood_sleep_threshold
        payload = self.codec.encode_method(method, params)  # TLError surfaces immediately

        refreshed = False
        attempt = 0
        while True:
            await self._respect_flood(method, threshold)
            for limiter in self.limiters:
                await limiter.acquire()
            generation = self._token_generation
            self.stats["calls"] += 1
            try:
                return await self._once(method, payload, kind, authorized, endpoint)
            except FloodWait as exc:
                self.stats["errors"] += 1
                self.stats["flood_waits"] += 1
                wait = float(exc.wait)
                self._flood_until[method] = time.monotonic() + wait
                await self._emit("flood_wait", method, wait)
                if wait > threshold or attempt >= max_retries:
                    raise
                log.info("%s: FloodWait %ss, sleeping", method, wait)
                await asyncio.sleep(wait + random.uniform(0.2, 0.8))
                attempt += 1
                self.stats["retries"] += 1
            except InvalidConstructor as exc:
                self.stats["errors"] += 1
                self._unsupported.add(method)
                raise MethodNotSupported(f"{method} was rejected as INVALID_CONSTRUCTOR") from exc
            except SessionExpired as exc:
                self.stats["errors"] += 1
                if not authorized or refreshed or not self.auto_refresh or isinstance(exc, SessionRevoked):
                    await self._emit("session_lost", exc)
                    raise
                refreshed = True
                if not await self._refresh_token(generation):
                    await self._emit("session_lost", exc)
                    raise
            except (TransportError, ServerError) as exc:
                self.stats["errors"] += 1
                if attempt >= max_retries:
                    raise
                delay = self.backoff_base * (2**attempt) + random.uniform(0, self.backoff_base)
                if exc.retry_after:
                    delay = max(delay, float(exc.retry_after))
                log.warning("%s failed (%s); retry %d/%d in %.1fs", method, exc.__class__.__name__, attempt + 1, max_retries, delay)
                await asyncio.sleep(delay)
                attempt += 1
                self.stats["retries"] += 1
            except RPCError:
                self.stats["errors"] += 1
                raise

    async def _respect_flood(self, method: str, threshold: float) -> None:
        remaining = self.flood_remaining(method)
        if remaining <= 0:
            return
        if remaining > threshold:
            # Fail fast without touching the server: the account was told to rest.
            raise FloodWait(420, f"FLOOD_WAIT_{int(remaining) + 1}", method)
        await asyncio.sleep(remaining)

    async def _once(self, method: str, payload: bytes, kind: str, authorized: bool, endpoint: str | None) -> TLValue:
        token = self.session.data.token or "" if authorized else ""
        envelope = self.codec.encode_constructor(
            "eitaaObject",
            {
                "token": token,
                "imei": self.session.data.imei,
                "packed_data": payload,
                "layer": self.profile.layer,
                "flags": self.profile.flags,
            },
        )
        raw = await self.transport.post(envelope, kind, endpoint=endpoint)
        try:
            return self.codec.decode_response(method, raw)
        except RPCError as exc:
            exc.method = exc.method or method
            raise
        except TLError:
            log.error("%s: undecodable response (%d bytes)", method, len(raw))
            raise

    # -------------------------------------------------------- token lifecycle
    async def refresh_token(self) -> bool:
        """Force a token refresh (single-flight)."""
        return await self._refresh_token(self._token_generation)

    async def _refresh_token(self, seen_generation: int) -> bool:
        async with self._refresh_lock:
            if self._token_generation != seen_generation:
                return True  # somebody else already refreshed while we waited
            try:
                payload = self.codec.encode_method("eitaaRefreshToken", {"app_info": self.profile.app_info()})
                result = await self._once("eitaaRefreshToken", payload, "client", True, None)
            except (SessionExpired, TransportError, RPCError, TLError) as exc:
                log.warning("token refresh failed: %s", exc)
                return False
            token = result.get("token") if isinstance(result, dict) else None
            if not token:
                return False
            data = self.session.data
            data.token = str(token)
            expire = result.get("expire") if isinstance(result, dict) else None
            data.token_expires_at = int(expire) if isinstance(expire, int) and expire > now() else None
            self._token_generation += 1
            self.stats["refreshes"] += 1
            await self.session.save()
            await self._emit("token_refreshed", data.token_expires_at)
            return True
