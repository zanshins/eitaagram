"""TL schema loader and index.

The bundled schema is Eitaa's layer-135 API (a Telegram derivative with a few
extra constructors such as ``eitaaObject``).  Definitions are pre-compiled so
the codec never has to parse type strings on the hot path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from ..errors import TLError

_OPTIONAL_RE = re.compile(r"^(?P<flag>[A-Za-z_]\w*)\.(?P<bit>\d+)\?(?P<base>.+)$")


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    type: str
    kind: str  # "flags" | "optional" | "plain"
    flag: str | None = None
    bit: int | None = None
    base: str = ""

    @classmethod
    def parse(cls, name: str, type_: str) -> Param:
        if type_ == "#":
            return cls(name, type_, "flags", base="#")
        match = _OPTIONAL_RE.match(type_)
        if match:
            return cls(
                name,
                type_,
                "optional",
                flag=match.group("flag"),
                bit=int(match.group("bit")),
                base=match.group("base").lstrip("!%"),
            )
        return cls(name, type_, "plain", base=type_.lstrip("!%"))


@dataclass(frozen=True, slots=True)
class Definition:
    id: int  # unsigned 32-bit
    name: str
    params: tuple[Param, ...]
    result_type: str
    is_method: bool

    @property
    def field_names(self) -> frozenset[str]:
        return frozenset(p.name for p in self.params)


class TLSchema:
    """Validated index over the TL schema JSON."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.layer: int = int(raw.get("layer", 0))
        self.methods_by_name: dict[str, Definition] = {}
        self.methods_by_id: dict[int, Definition] = {}
        self.constructors_by_name: dict[str, Definition] = {}
        self.constructors_by_id: dict[int, Definition] = {}
        self.constructors_by_type: dict[str, list[Definition]] = {}
        self._index()

    @classmethod
    def bundled(cls) -> TLSchema:
        path = files("eitaagram.data").joinpath("eitaa-schema.json")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def _index(self) -> None:
        # MTProto first, API second: on a name clash (e.g. ``message``) the API
        # definition wins by name, while both stay reachable by constructor id.
        for section in ("MTProto", "API"):
            data = self.raw.get(section)
            if not isinstance(data, dict):
                raise TLError(f"schema section {section!r} is missing")
            for item in data.get("constructors", []):
                definition = self._build(item, is_method=False)
                if definition is None:
                    continue
                self.constructors_by_name[definition.name] = definition
                self.constructors_by_id[definition.id] = definition
            for item in data.get("methods", []):
                definition = self._build(item, is_method=True)
                if definition is None:
                    continue
                self.methods_by_name[definition.name] = definition
                self.methods_by_id[definition.id] = definition
        for definition in self.constructors_by_name.values():
            self.constructors_by_type.setdefault(definition.result_type, []).append(definition)

    @staticmethod
    def _build(item: dict[str, Any], *, is_method: bool) -> Definition | None:
        name = item.get("predicate") or item.get("method")
        if not name:
            return None
        ident = item.get("id")
        if not isinstance(ident, int):
            raise TLError(f"invalid id for {name}")
        params = tuple(Param.parse(p["name"], p["type"]) for p in item.get("params", []))
        return Definition(
            id=ident & 0xFFFFFFFF,
            name=name,
            params=params,
            result_type=item.get("type", "Object"),
            is_method=is_method,
        )

    def method(self, name: str) -> Definition:
        try:
            return self.methods_by_name[name]
        except KeyError:
            raise TLError(f"unknown TL method: {name}") from None

    def constructor(self, name: str) -> Definition:
        try:
            return self.constructors_by_name[name]
        except KeyError:
            raise TLError(f"unknown TL constructor: {name}") from None
