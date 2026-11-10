"""Schema-driven TL binary codec.

Objects are plain ``dict`` values carrying the constructor name under ``"_"``::

    {"_": "inputPeerUser", "user_id": 1, "access_hash": 2}

Methods are encoded from ``(name, params)``; responses are decoded by the
method's declared result type.  Eitaa-specific "markers" that replace a result
(``error``, ``rpc_error``, ``eitaa_updates_expire_token``, ``eitaa_token_updating``)
are converted to exceptions here so callers only ever see real results.
"""

from __future__ import annotations

import gzip
import logging
import re
import struct
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import RPCError, TLError, TokenExpired, TokenUpdating
from .schema import Definition, TLSchema

log = logging.getLogger("eitaagram.tl")

TLValue = Any
TLObject = dict[str, Any]

VECTOR_ID = 0x1CB5C415
BOOL_TRUE = 0x997275B5
BOOL_FALSE = 0xBC799737
_MAX_VECTOR = 1_000_000
_VECTOR_RE = re.compile(r"^[Vv]ector<(?P<item>.+)>$")

_I32 = struct.Struct("<i")
_U32 = struct.Struct("<I")
_I64 = struct.Struct("<q")
_F64 = struct.Struct("<d")


class Reader:
    __slots__ = ("data", "offset")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def take(self, count: int) -> bytes:
        end = self.offset + count
        if count < 0 or end > len(self.data):
            raise TLError(
                f"truncated TL data: need {count} bytes at offset {self.offset}, "
                f"total {len(self.data)}"
            )
        out = self.data[self.offset : end]
        self.offset = end
        return out

    def u32(self) -> int:
        return int(_U32.unpack(self.take(4))[0])

    def i32(self) -> int:
        return int(_I32.unpack(self.take(4))[0])

    def i64(self) -> int:
        return int(_I64.unpack(self.take(8))[0])

    def f64(self) -> float:
        return float(_F64.unpack(self.take(8))[0])

    def tl_bytes(self) -> bytes:
        first = self.take(1)[0]
        header = 1
        if first == 254:
            length = int.from_bytes(self.take(3), "little")
            header = 4
        else:
            length = first
        payload = self.take(length)
        padding = (-(header + length)) % 4
        if padding:
            self.take(padding)
        return payload


def encode_tl_bytes(value: bytes) -> bytes:
    length = len(value)
    if length < 254:
        prefix = bytes((length,))
    elif length <= 0xFFFFFF:
        prefix = b"\xfe" + length.to_bytes(3, "little")
    else:
        raise TLError(f"TL bytes value too large: {length}")
    blob = prefix + value
    return blob + b"\x00" * ((-len(blob)) % 4)


class TLCodec:
    """Encode methods / constructors and decode results using a :class:`TLSchema`."""

    def __init__(self, schema: TLSchema | None = None) -> None:
        self.schema = schema or TLSchema.bundled()
        self._id_error = self._ctor_id("error")
        self._id_rpc_error = self._ctor_id("rpc_error")
        self._id_gzip = self._ctor_id("gzip_packed")
        self._id_rpc_result = self._ctor_id("rpc_result")
        self._id_expire = self._ctor_id("eitaa_updates_expire_token")
        self._id_updating = self._ctor_id("eitaa_token_updating")

    def _ctor_id(self, name: str) -> int | None:
        definition = self.schema.constructors_by_name.get(name)
        return definition.id if definition else None

    # ------------------------------------------------------------------ encode
    def encode_method(self, method: str, params: Mapping[str, Any] | None = None) -> bytes:
        return self._encode_definition(self.schema.method(method), params or {})

    def encode_constructor(self, predicate: str, values: Mapping[str, Any] | None = None) -> bytes:
        return self._encode_definition(self.schema.constructor(predicate), values or {})

    def _encode_definition(self, definition: Definition, values: Mapping[str, Any]) -> bytes:
        unknown = set(values) - definition.field_names - {"_"}
        if unknown:
            raise TLError(
                f"{definition.name}: unexpected field(s) {sorted(unknown)}; "
                f"valid fields: {sorted(definition.field_names)}"
            )
        flags = self._compute_flags(definition, values)
        out = bytearray(_U32.pack(definition.id))
        for param in definition.params:
            if param.kind == "flags":
                out += _U32.pack(flags[param.name] & 0xFFFFFFFF)
            elif param.kind == "optional":
                assert param.flag is not None and param.bit is not None
                if not flags.get(param.flag, 0) & (1 << param.bit):
                    continue
                if param.base == "true":
                    continue
                out += self._encode_type(param.base, values.get(param.name), f"{definition.name}.{param.name}")
            else:
                if param.name not in values:
                    raise TLError(f"{definition.name}: missing required field {param.name!r} ({param.type})")
                out += self._encode_type(param.base, values[param.name], f"{definition.name}.{param.name}")
        return bytes(out)

    @staticmethod
    def _compute_flags(definition: Definition, values: Mapping[str, Any]) -> dict[str, int]:
        flags: dict[str, int] = {}
        for param in definition.params:
            if param.kind == "flags":
                explicit = values.get(param.name, 0)
                flags[param.name] = int(explicit) if isinstance(explicit, int) else 0
        for param in definition.params:
            if param.kind != "optional":
                continue
            assert param.flag is not None and param.bit is not None
            value = values.get(param.name)
            present = bool(value) if param.base == "true" else value is not None
            if present:
                flags[param.flag] = flags.get(param.flag, 0) | (1 << param.bit)
        return flags

    def _encode_type(self, type_name: str, value: Any, where: str) -> bytes:
        match = _VECTOR_RE.match(type_name)
        if match:
            if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
                raise TLError(f"{where}: expected a sequence for {type_name}")
            item_type = match.group("item").lstrip("!%")
            out = bytearray(_U32.pack(VECTOR_ID))
            out += _I32.pack(len(value))
            for item in value:
                out += self._encode_type(item_type, item, where)
            return bytes(out)
        try:
            if type_name in ("int", "#"):
                return _I32.pack(_as_int(value, where))
            if type_name == "long":
                return _I64.pack(_as_int(value, where))
            if type_name == "double":
                return _F64.pack(float(value))
            if type_name == "string":
                if not isinstance(value, str):
                    raise TLError(f"{where}: expected str, got {type(value).__name__}")
                return encode_tl_bytes(value.encode("utf-8"))
            if type_name == "bytes":
                return encode_tl_bytes(_as_bytes(value, where))
            if type_name == "int128":
                return _fixed(value, 16, where)
            if type_name == "int256":
                return _fixed(value, 32, where)
            if type_name == "Bool":
                return _U32.pack(BOOL_TRUE if value else BOOL_FALSE)
            if type_name == "true":
                return b""
        except struct.error as exc:
            raise TLError(f"{where}: value out of range ({exc})") from exc
        return self._encode_object(value, type_name, where)

    def _encode_object(self, value: Any, expected: str, where: str) -> bytes:
        if isinstance(value, str):
            predicate, values = value, {}
        elif isinstance(value, Mapping):
            predicate, values = str(value.get("_") or ""), value
        else:
            raise TLError(f"{where}: expected a constructor dict like {{'_': 'inputPeerSelf'}}")
        if not predicate:
            raise TLError(f"{where}: missing constructor key '_'")
        definition = self.schema.constructor(predicate)
        if expected not in ("Object", "X"):
            allowed = self.schema.constructors_by_type.get(expected)
            if allowed and definition not in allowed:
                raise TLError(f"{where}: {predicate} is {definition.result_type}, expected {expected}")
        return self._encode_definition(definition, values)

    # ------------------------------------------------------------------ decode
    def decode_response(self, method: str, payload: bytes, *, strict: bool = True) -> TLValue:
        """Decode the response of ``method``; marker/error payloads raise."""
        definition = self.schema.method(method)
        payload = self._unwrap(payload, method)
        reader = Reader(payload)
        value = self._decode_type(reader, definition.result_type.lstrip("!%"), method)
        if strict and reader.remaining:
            log.warning(
                "%s: %d unread trailing byte(s) ignored: %s",
                method, reader.remaining, reader.data[reader.offset:].hex()
            )
        return value

    def decode_any(self, payload: bytes) -> TLValue:
        reader = Reader(self._unwrap(payload, None))
        return self._decode_type(reader, "Object", "")

    def decode_request(self, method: str, payload: bytes) -> TLObject:
        """Decode a serialized request (diagnostics / capture-based tests)."""
        definition = self.schema.method(method)
        reader = Reader(payload)
        ident = reader.u32()
        if ident != definition.id:
            raise TLError(f"request id 0x{ident:08x} does not match {method}")
        return self._decode_params(reader, definition, method)

    def _unwrap(self, payload: bytes, method: str | None) -> bytes:
        """Raise on error/marker payloads and strip gzip / rpc_result wrappers."""
        for _ in range(8):  # bounded: gzip bombs / pathological nesting
            if len(payload) < 4:
                return payload
            ident = _U32.unpack_from(payload)[0]
            if ident == self._id_error or ident == self._id_rpc_error:
                obj = self.decode_any_raw(payload)
                if ident == self._id_rpc_error:
                    raise RPCError.build(int(obj.get("error_code", 0)), str(obj.get("error_message", "")), method)
                raise RPCError.build(int(obj.get("code", 0)), str(obj.get("text", "")), method)
            if ident == self._id_expire:
                raise TokenExpired(401, "EITAA_UPDATES_EXPIRE_TOKEN", method)
            if ident == self._id_updating:
                raise TokenUpdating(401, "EITAA_TOKEN_UPDATING", method)
            if ident == self._id_gzip:
                reader = Reader(payload)
                reader.u32()
                packed = reader.tl_bytes()
                try:
                    payload = gzip.decompress(packed)
                except OSError as exc:
                    raise TLError("invalid gzip_packed payload") from exc
                continue
            if ident == self._id_rpc_result:
                payload = payload[12:]
                continue
            return payload
        raise TLError("too many nested wrappers in response")

    def decode_any_raw(self, payload: bytes) -> TLObject:
        reader = Reader(payload)
        value = self._decode_object(reader, "Object", "")
        return value  # type: ignore[no-any-return]

    def _decode_type(self, reader: Reader, type_name: str, path: str) -> TLValue:
        match = _VECTOR_RE.match(type_name)
        if match:
            ident = reader.u32()
            if ident != VECTOR_ID:
                raise TLError(f"{path}: expected vector (0x{VECTOR_ID:08x}), got 0x{ident:08x} at {reader.offset - 4}")
            count = reader.i32()
            if not 0 <= count <= _MAX_VECTOR:
                raise TLError(f"{path}: invalid vector length {count}")
            item_type = match.group("item").lstrip("!%")
            return [self._decode_type(reader, item_type, f"{path}[{i}]") for i in range(count)]
        if type_name in ("int", "#"):
            return reader.i32()
        if type_name == "long":
            return reader.i64()
        if type_name == "double":
            return reader.f64()
        if type_name == "string":
            return reader.tl_bytes().decode("utf-8", errors="replace")
        if type_name == "bytes":
            return reader.tl_bytes()
        if type_name == "int128":
            return reader.take(16)
        if type_name == "int256":
            return reader.take(32)
        if type_name == "Bool":
            ident = reader.u32()
            if ident == BOOL_TRUE:
                return True
            if ident == BOOL_FALSE:
                return False
            raise TLError(f"{path}: invalid Bool constructor 0x{ident:08x}")
        if type_name == "true":
            return True
        return self._decode_object(reader, type_name, path)

    def _decode_object(self, reader: Reader, expected: str, path: str) -> TLValue:
        start = reader.offset
        ident = reader.u32()
        if ident == VECTOR_ID:  # boxed vector where the type is only "Object"
            reader.offset = start
            return self._decode_type(reader, "Vector<Object>", path)
        if ident == self._id_gzip:
            packed = reader.tl_bytes()
            try:
                inner = gzip.decompress(packed)
            except OSError as exc:
                raise TLError(f"{path}: invalid gzip_packed payload") from exc
            return self._decode_type(Reader(inner), expected, path)
        definition = self.schema.constructors_by_id.get(ident)
        if definition is None:
            raise TLError(f"{path or expected}: unknown constructor 0x{ident:08x} at offset {start}")
        return {"_": definition.name, **self._decode_params(reader, definition, path or definition.name)}

    def _decode_params(self, reader: Reader, definition: Definition, path: str) -> TLObject:
        result: TLObject = {}
        flags: dict[str, int] = {}
        for param in definition.params:
            where = f"{path}.{param.name}"
            if param.kind == "flags":
                value = reader.u32()
                flags[param.name] = value
                result[param.name] = value
            elif param.kind == "optional":
                assert param.flag is not None and param.bit is not None
                if not flags.get(param.flag, 0) & (1 << param.bit):
                    continue
                result[param.name] = True if param.base == "true" else self._decode_type(reader, param.base, where)
            else:
                result[param.name] = self._decode_type(reader, param.base, where)
        return result


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    raise TLError(f"{where}: expected int, got {type(value).__name__}")


def _as_bytes(value: Any, where: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise TLError(f"{where}: expected bytes, got {type(value).__name__}")


def _fixed(value: Any, size: int, where: str) -> bytes:
    if isinstance(value, int):
        return value.to_bytes(size, "little", signed=False)
    raw = _as_bytes(value, where)
    if len(raw) != size:
        raise TLError(f"{where}: expected exactly {size} bytes")
    return raw
