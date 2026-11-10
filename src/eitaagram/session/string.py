from __future__ import annotations

import base64
import zlib
from typing import Any

from .base import Session, SessionData, dumps, loads

_PREFIX = "eg1:"


class StringSession(Session):
    """Portable session serialized into one opaque string.

    Ideal for storing hundreds of accounts in *your own* database::

        s = StringSession()              # new
        s = StringSession(saved_string)  # restore
        ...
        saved_string = s.export()

    The string contains the account token: treat it like a password.
    """

    def __init__(self, value: str | None = None, *, name: str = "string", include_peers: bool = True) -> None:
        super().__init__(name)
        self._include_peers = include_peers
        self._raw: dict[str, Any] | None = None
        if value:
            self._raw = self.decode(value)
            self.data = SessionData.from_dict(self._raw, name=name)  # usable before load()
            self._dirty = False

    @staticmethod
    def decode(value: str) -> dict[str, Any]:
        if not value.startswith(_PREFIX):
            raise ValueError("not an eitaagram session string")
        try:
            return loads(zlib.decompress(base64.urlsafe_b64decode(value[len(_PREFIX) :].encode("ascii"))))
        except Exception as exc:
            raise ValueError("corrupted session string") from exc

    def export(self) -> str:
        raw = self.data.to_dict(include_peers=self._include_peers)
        blob = zlib.compress(dumps(raw), 9)
        return _PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")

    async def _read(self) -> dict[str, Any] | None:
        return self._raw

    async def _write(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    async def _remove(self) -> None:
        self._raw = None
