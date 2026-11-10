"""Minimal HTML -> (text, entities) converter with UTF-16 offsets."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

_SIMPLE = {
    "b": "messageEntityBold",
    "strong": "messageEntityBold",
    "i": "messageEntityItalic",
    "em": "messageEntityItalic",
    "u": "messageEntityUnderline",
    "ins": "messageEntityUnderline",
    "s": "messageEntityStrike",
    "strike": "messageEntityStrike",
    "del": "messageEntityStrike",
    "code": "messageEntityCode",
    "pre": "messageEntityPre",
}


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0
        self.stack: list[tuple[str, int, dict[str, str | None]]] = []
        self.entities: list[dict[str, Any]] = []

    def _add(self, text: str) -> None:
        self.parts.append(text)
        self.length += utf16_len(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._add("\n")
        elif tag in _SIMPLE or tag == "a":
            self.stack.append((tag, self.length, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                _, start, attrs = self.stack.pop(index)
                self._close(tag, start, attrs)
                return

    def _close(self, tag: str, start: int, attrs: dict[str, str | None]) -> None:
        length = self.length - start
        if length <= 0:
            return
        if tag == "a":
            href = attrs.get("href")
            if href:
                self.entities.append({"_": "messageEntityTextUrl", "offset": start, "length": length, "url": href})
        elif tag == "pre":
            self.entities.append({"_": "messageEntityPre", "offset": start, "length": length, "language": ""})
        else:
            self.entities.append({"_": _SIMPLE[tag], "offset": start, "length": length})

    def handle_data(self, data: str) -> None:
        self._add(data)

    def finish(self) -> tuple[str, list[dict[str, Any]]]:
        self.close()
        while self.stack:
            tag, start, attrs = self.stack.pop()
            self._close(tag, start, attrs)
        self.entities.sort(key=lambda e: (e["offset"], -e["length"]))
        return "".join(self.parts), self.entities


def parse_html(text: str) -> tuple[str, list[dict[str, Any]]]:
    builder = _Builder()
    builder.feed(text)
    return builder.finish()
