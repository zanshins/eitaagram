"""HTTPS transport with endpoint health tracking and failover."""

from __future__ import annotations

import logging
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable

from .errors import TransportError
from .profile import ClientProfile

log = logging.getLogger("eitaagram.transport")


@runtime_checkable
class Transport(Protocol):
    """Anything that can deliver one serialized envelope and return the reply."""

    async def post(self, payload: bytes, kind: str = "client", *, endpoint: str | None = None) -> bytes: ...

    def pick_endpoint(self, kind: str = "client") -> str: ...

    async def close(self) -> None: ...


class EndpointPool:
    """Healthy-first random ordering with exponential cooldown for failing hosts."""

    def __init__(self, endpoints: tuple[str, ...], *, base_cooldown: float = 15.0, max_cooldown: float = 300.0) -> None:
        self.endpoints = endpoints
        self._base = base_cooldown
        self._max = max_cooldown
        self._failures: dict[str, int] = {}
        self._until: dict[str, float] = {}
        self._rng = random.SystemRandom()

    def order(self) -> list[str]:
        now = time.monotonic()
        healthy = [e for e in self.endpoints if self._until.get(e, 0.0) <= now]
        cooling = sorted((e for e in self.endpoints if self._until.get(e, 0.0) > now), key=lambda e: self._until[e])
        self._rng.shuffle(healthy)
        return healthy + cooling

    def failure(self, endpoint: str, *, hold: float | None = None) -> None:
        count = self._failures.get(endpoint, 0) + 1
        self._failures[endpoint] = count
        delay = hold if hold is not None else min(self._max, self._base * (2 ** (count - 1)))
        self._until[endpoint] = time.monotonic() + delay

    def success(self, endpoint: str) -> None:
        self._failures.pop(endpoint, None)
        self._until.pop(endpoint, None)


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max(0, int((target - datetime.now(UTC)).total_seconds()))


class HttpTransport:
    """Binary POST transport over ``httpx`` (HTTP/2 when available).

    * One instance per account when you need a per-account ``proxy``; pass a
      shared ``client`` to multiplex many accounts over one connection pool.
    * ``endpoint=`` pins a request to one host (required for multi-part uploads).
    * Payloads and tokens are never logged.
    """

    def __init__(
        self,
        profile: ClientProfile,
        *,
        proxy: str | None = None,
        timeout: float = 45.0,
        http2: bool = True,
        client: Any | None = None,
        max_connections: int = 20,
    ) -> None:
        self.profile = profile
        self._pools = {k: EndpointPool(profile.endpoints(k)) for k in ("client", "upload", "download")}
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError("HttpTransport needs httpx: pip install 'httpx[http2]'") from exc
        if http2:
            try:
                import h2  # noqa: F401
            except ImportError:
                http2 = False
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout),
            "follow_redirects": False,
            "http2": http2,
            "headers": profile.headers(),
            "limits": httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        }
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)

    def pick_endpoint(self, kind: str = "client") -> str:
        return self._pools[kind].order()[0]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def post(self, payload: bytes, kind: str = "client", *, endpoint: str | None = None) -> bytes:
        pool = self._pools[kind]
        candidates = [endpoint] if endpoint else pool.order()
        problems: list[str] = []
        waits: list[int] = []
        for url in candidates:
            host = url.split("/eitaa", 1)[0]
            try:
                response = await self._client.post(url, content=payload)
            except Exception as exc:  # httpx.HTTPError family; kept broad on purpose
                pool.failure(url)
                problems.append(f"{host}: {exc.__class__.__name__}")
                continue
            if response.status_code != 200:
                wait = _retry_after(response.headers.get("retry-after"))
                pool.failure(url, hold=float(wait) if wait else None)
                if wait is not None:
                    waits.append(wait)
                problems.append(f"{host}: HTTP {response.status_code}")
                continue
            if not response.content:
                pool.failure(url)
                problems.append(f"{host}: empty response")
                continue
            pool.success(url)
            return bytes(response.content)
        raise TransportError(
            f"all {kind} endpoints failed: {'; '.join(problems) or 'none configured'}",
            retry_after=max(waits, default=None),
        )
