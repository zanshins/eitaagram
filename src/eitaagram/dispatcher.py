"""Handler registry and bounded-concurrency dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .filters import Filter

if TYPE_CHECKING:  # pragma: no cover
    from .client import Client

log = logging.getLogger("eitaagram.dispatcher")

Callback = Callable[..., Awaitable[Any] | Any]


class StopPropagation(Exception):
    """Raise inside a handler to skip every later handler group."""


class ContinuePropagation(Exception):
    """Raise inside a handler to also run the next matching handler of its group."""


class Handler:
    kind = "message"

    def __init__(self, callback: Callback, filters: Filter | None = None) -> None:
        self.callback = callback
        self.filters = filters

    async def matches(self, client: Client, payload: Any) -> bool:
        if self.filters is None:
            return True
        return await self.filters(client, payload)


class MessageHandler(Handler):
    kind = "message"


class EditedMessageHandler(Handler):
    kind = "edited_message"


class DeletedMessagesHandler(Handler):
    """Callback receives ``(client, DeletedMessages)``; filters are not applied."""

    kind = "deleted_messages"

    async def matches(self, client: Client, payload: Any) -> bool:
        return True


class RawUpdateHandler(Handler):
    """Callback receives ``(client, raw_update_dict)``; filters are not applied."""

    kind = "raw"

    async def matches(self, client: Client, payload: Any) -> bool:
        return True


class Dispatcher:
    def __init__(self, client: Client, *, workers: int = 4) -> None:
        self.client = client
        self.workers = max(1, workers)
        self._groups: dict[int, list[Handler]] = {}
        self._sem = asyncio.Semaphore(self.workers)
        self._tasks: set[asyncio.Task[None]] = set()
        self.on_error: Callable[[BaseException], None] | None = None

    def add_handler(self, handler: Handler, group: int = 0) -> Handler:
        self._groups.setdefault(group, []).append(handler)
        self._groups = dict(sorted(self._groups.items()))
        return handler

    def remove_handler(self, handler: Handler, group: int = 0) -> None:
        try:
            self._groups[group].remove(handler)
        except (KeyError, ValueError):
            pass

    def dispatch(self, kind: str, payload: Any) -> None:
        """Schedule delivery; never blocks the update loop."""
        task = asyncio.create_task(self._deliver(kind, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, kind: str, payload: Any) -> None:
        async with self._sem:
            for handlers in list(self._groups.values()):
                stop = False
                for handler in list(handlers):
                    if handler.kind != kind:
                        continue
                    try:
                        if not await handler.matches(self.client, payload):
                            continue
                        result = handler.callback(self.client, payload)
                        if inspect.isawaitable(result):
                            await result
                    except ContinuePropagation:
                        continue
                    except StopPropagation:
                        stop = True
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.exception("handler %r failed", getattr(handler.callback, "__name__", handler.callback))
                        if self.on_error:
                            self.on_error(exc)
                    break  # first match wins inside a group
                if stop:
                    return

    async def drain(self, timeout: float | None = 10.0) -> None:
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(set(self._tasks), timeout=timeout)
        for task in pending:
            task.cancel()
