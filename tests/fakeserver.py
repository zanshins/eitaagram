"""In-process fake Eitaa gateway used by the test-suite.

It speaks the real binary protocol: it decodes the ``eitaaObject`` envelope and
the inner TL request with the library's own codec (so request layout bugs are
caught), then answers with a handler-provided value encoded back to TL.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eitaagram.errors import TransportError
from eitaagram.tl import Reader, TLCodec
from eitaagram.tl.codec import BOOL_FALSE, BOOL_TRUE, VECTOR_ID

_CODEC = TLCodec()


@dataclass
class RpcFail:
    code: int
    text: str


@dataclass
class Marker:
    name: str  # "eitaa_updates_expire_token" | "eitaa_token_updating"


@dataclass
class TransportFail:
    message: str = "boom"


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    token: str
    imei: str
    layer: int
    flags: int
    kind: str
    endpoint: str | None


def encode_value(codec: TLCodec, value: Any) -> bytes:
    if isinstance(value, bool):
        return struct.pack("<I", BOOL_TRUE if value else BOOL_FALSE)
    if isinstance(value, list):
        out = bytearray(struct.pack("<Ii", VECTOR_ID, len(value)))
        for item in value:
            out += encode_value(codec, item)
        return bytes(out)
    if isinstance(value, dict):
        return codec.encode_constructor(value["_"], {k: v for k, v in value.items() if k != "_"})
    raise TypeError(f"cannot encode {value!r}")


class FakeEitaa:
    def __init__(self) -> None:
        self.codec = _CODEC
        self.handlers: dict[str, Callable[[dict[str, Any], Call], Any]] = {}
        self.calls: list[Call] = []
        self.closed = False
        self.pinned: list[str | None] = []

    def on(self, method: str, handler: Callable[[dict[str, Any], Call], Any] | Any) -> None:
        self.handlers[method] = handler if callable(handler) else (lambda _p, _c, v=handler: v)

    def count(self, method: str) -> int:
        return sum(1 for c in self.calls if c.method == method)

    def of(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    # -- Transport protocol -------------------------------------------------
    def pick_endpoint(self, kind: str = "client") -> str:
        return f"https://fake-{kind}.example/eitaa/"

    async def close(self) -> None:
        self.closed = True

    async def post(self, payload: bytes, kind: str = "client", *, endpoint: str | None = None) -> bytes:
        envelope = self.codec.decode_any_raw(payload)
        assert envelope["_"] == "eitaaObject", envelope
        inner: bytes = envelope["packed_data"]
        ident = struct.unpack_from("<I", inner)[0]
        definition = self.codec.schema.methods_by_id[ident]
        params = self.codec.decode_request(definition.name, inner)
        call = Call(
            definition.name, params, envelope["token"], envelope["imei"], envelope["layer"], envelope["flags"], kind, endpoint
        )
        self.calls.append(call)
        self.pinned.append(endpoint)
        handler = self.handlers.get(definition.name)
        if handler is None:
            return self.codec.encode_constructor("error", {"code": 400, "text": "METHOD_NOT_MOCKED"})
        result = handler(params, call)
        if isinstance(result, TransportFail):
            raise TransportError(result.message)
        if isinstance(result, RpcFail):
            return self.codec.encode_constructor("error", {"code": result.code, "text": result.text})
        if isinstance(result, Marker):
            return self.codec.encode_constructor(result.name)
        if isinstance(result, bytes):
            return result
        return encode_value(self.codec, result)


def user(ident: int = 1, **extra: Any) -> dict[str, Any]:
    base = {"_": "user", "id": ident, "access_hash": 1000 + ident, "first_name": f"User{ident}"}
    base.update(extra)
    return base


def authorization(token: str = "TOKEN-1", ident: int = 1) -> dict[str, Any]:
    return {"_": "auth.authorization", "token": token, "user": user(ident, self=True)}


def sent_code(hash_: str = "HASH1", kind: str = "auth.sentCodeTypeSms", length: int = 5) -> dict[str, Any]:
    return {"_": "auth.sentCode", "type": {"_": kind, "length": length}, "phone_code_hash": hash_}


def message(msg_id: int, peer: dict[str, Any], text: str = "hi", *, out: bool = False, date: int = 1_700_000_000, from_id: dict | None = None, **extra: Any) -> dict[str, Any]:
    raw = {"_": "message", "id": msg_id, "peer_id": peer, "date": date, "message": text}
    if out:
        raw["out"] = True
    if from_id:
        raw["from_id"] = from_id
    raw.update(extra)
    return raw


def state(pts: int = 10, date: int = 1_700_000_000, seq: int = 1) -> dict[str, Any]:
    return {"_": "updates.state", "pts": pts, "qts": 0, "date": date, "seq": seq, "unread_count": 0}


def diff_empty(date: int = 1_700_000_000, seq: int = 1) -> dict[str, Any]:
    return {"_": "updates.differenceEmpty", "date": date, "seq": seq}


def difference(messages: list, others: list | None = None, users: list | None = None, chats: list | None = None, st: dict | None = None, slice_: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "new_messages": messages,
        "new_encrypted_messages": [],
        "other_updates": others or [],
        "chats": chats or [],
        "users": users or [],
    }
    if slice_:
        return {"_": "updates.differenceSlice", **body, "intermediate_state": st or state()}
    return {"_": "updates.difference", **body, "state": st or state()}
