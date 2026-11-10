"""Composable message filters (``filters.private & filters.text & ~filters.me``)."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .client import Client
    from .types import Message


class Filter:
    """Base class: ``await flt(client, message)`` -> ``bool``."""

    async def __call__(self, client: Client, message: Message) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def __and__(self, other: Filter) -> Filter:
        return _And(self, other)

    def __or__(self, other: Filter) -> Filter:
        return _Or(self, other)

    def __invert__(self) -> Filter:
        return _Not(self)


class _And(Filter):
    def __init__(self, a: Filter, b: Filter) -> None:
        self.a, self.b = a, b

    async def __call__(self, client: Client, message: Message) -> bool:
        return await self.a(client, message) and await self.b(client, message)


class _Or(Filter):
    def __init__(self, a: Filter, b: Filter) -> None:
        self.a, self.b = a, b

    async def __call__(self, client: Client, message: Message) -> bool:
        return await self.a(client, message) or await self.b(client, message)


class _Not(Filter):
    def __init__(self, a: Filter) -> None:
        self.a = a

    async def __call__(self, client: Client, message: Message) -> bool:
        return not await self.a(client, message)


class _Func(Filter):
    def __init__(self, func: Callable[..., Any], name: str | None = None) -> None:
        self.func = func
        self.name = name or getattr(func, "__name__", "custom")

    async def __call__(self, client: Client, message: Message) -> bool:
        result = self.func(client, message)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def __repr__(self) -> str:
        return f"<Filter {self.name}>"


def create(func: Callable[[Any, Any], bool | Awaitable[bool]], name: str | None = None) -> Filter:
    """Build a filter from ``func(client, message)`` (sync or async)."""
    return _Func(func, name)


def _simple(name: str, pred: Callable[[Message], bool]) -> Filter:
    return _Func(lambda _c, m: pred(m), name)


all = _simple("all", lambda m: True)
private = _simple("private", lambda m: m.chat.kind == "private")
group = _simple("group", lambda m: m.chat.kind in ("group", "supergroup"))
supergroup = _simple("supergroup", lambda m: m.chat.kind == "supergroup")
channel = _simple("channel", lambda m: m.chat.kind == "channel")
incoming = _simple("incoming", lambda m: not m.outgoing)
outgoing = _simple("outgoing", lambda m: m.outgoing)
text = _simple("text", lambda m: bool(m.text))
media = _simple("media", lambda m: m.media is not None)
photo = _simple("photo", lambda m: m.media == "photo")
video = _simple("video", lambda m: m.media in ("video", "animation"))
audio = _simple("audio", lambda m: m.media == "audio")
voice = _simple("voice", lambda m: m.media == "voice")
document = _simple("document", lambda m: m.media == "document")
sticker = _simple("sticker", lambda m: m.media == "sticker")
reply = _simple("reply", lambda m: m.reply_to_message_id is not None)
forwarded = _simple("forwarded", lambda m: m.forward_date is not None)
service = _simple("service", lambda m: m.is_service)
me = _Func(lambda c, m: m.sender_id is not None and m.sender_id == c.me_id, "me")


def command(commands: str | Iterable[str], prefixes: str | Iterable[str] = "/", case_sensitive: bool = False) -> Filter:
    names = [commands] if isinstance(commands, str) else list(commands)
    pfx = [prefixes] if isinstance(prefixes, str) else list(prefixes)
    wanted = {n if case_sensitive else n.lower() for n in names}

    def check(_c: Any, m: Message) -> bool:
        if not m.text:
            return False
        for prefix in pfx:
            if m.text.startswith(prefix):
                head = m.text[len(prefix) :].split(maxsplit=1)[0].split("@", 1)[0] if m.text[len(prefix) :].strip() else ""
                if (head if case_sensitive else head.lower()) in wanted:
                    return True
        return False

    return _Func(check, f"command({sorted(wanted)})")


def regex(pattern: str | re.Pattern[str], flags: int = 0) -> Filter:
    compiled = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

    def check(_c: Any, m: Message) -> bool:
        if not m.text:
            return False
        match = compiled.search(m.text)
        m.matches = match
        return match is not None

    return _Func(check, f"regex({compiled.pattern!r})")


def user(ids: int | str | Iterable[int | str]) -> Filter:
    items = [ids] if isinstance(ids, (int, str)) else list(ids)
    numeric = {i for i in items if isinstance(i, int)}
    names = {str(i).lstrip("@").lower() for i in items if isinstance(i, str)}

    def check(_c: Any, m: Message) -> bool:
        if m.sender_id in numeric:
            return True
        return bool(m.from_user and m.from_user.username and m.from_user.username.lower() in names)

    return _Func(check, "user")


def chat(ids: int | str | Iterable[int | str]) -> Filter:
    items = [ids] if isinstance(ids, (int, str)) else list(ids)
    numeric = {i for i in items if isinstance(i, int)}
    names = {str(i).lstrip("@").lower() for i in items if isinstance(i, str)}

    def check(_c: Any, m: Message) -> bool:
        if m.chat.id in numeric:
            return True
        return bool(m.chat.username and m.chat.username.lower() in names)

    return _Func(check, "chat")


def account(names: str | Iterable[str]) -> Filter:
    """Pool helper: only messages received by the named account(s)."""
    wanted = {names} if isinstance(names, str) else set(names)
    return _Func(lambda c, m: c.name in wanted, "account")


__all__ = [
    "Filter", "create", "all", "private", "group", "supergroup", "channel", "incoming", "outgoing",
    "text", "media", "photo", "video", "audio", "voice", "document", "sticker", "reply", "forwarded",
    "service", "me", "command", "regex", "user", "chat", "account",
]
