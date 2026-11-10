"""Small helpers shared across the package."""

from __future__ import annotations

import re
import secrets
import string
import time
from typing import Any

_ALNUM = string.ascii_lowercase + string.digits
_TOKEN_RE = re.compile(r"(token['\"=:\s]+)([A-Za-z0-9_\-:.]{8,})", re.IGNORECASE)

# Chat-id convention (same idea as Pyrogram): users are positive, basic chats
# are ``-id`` and channels / supergroups are ``-1000000000000 - id``.
CHANNEL_ID_OFFSET = 1_000_000_000_000


def random_id() -> int:
    """Random positive 63-bit integer used for ``random_id`` / file ids."""
    return secrets.randbits(63)


def generate_imei() -> str:
    """Stable per-account device identifier in the format the web client uses."""
    return "".join(secrets.choice(_ALNUM) for _ in range(16)) + "__web"


def now() -> int:
    return int(time.time())


def normalize_phone(phone: str) -> str:
    """Digits only, international form (``0912...`` -> ``98912...``)."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = "98" + digits[1:]
    if not digits:
        raise ValueError("phone number is empty")
    return digits


def peer_to_chat_id(kind: str, raw_id: int) -> int:
    if kind == "user":
        return raw_id
    if kind == "chat":
        return -raw_id
    if kind == "channel":
        return -CHANNEL_ID_OFFSET - raw_id
    raise ValueError(f"unknown peer kind: {kind}")


def chat_id_to_peer(chat_id: int) -> tuple[str, int]:
    """Inverse of :func:`peer_to_chat_id`."""
    if chat_id >= 0:
        return "user", chat_id
    if chat_id <= -CHANNEL_ID_OFFSET:
        return "channel", -chat_id - CHANNEL_ID_OFFSET
    return "chat", -chat_id


def redact(text: str) -> str:
    """Mask anything that looks like an access token before logging."""
    return _TOKEN_RE.sub(lambda m: m.group(1) + m.group(2)[:4] + "…", text)


def get(obj: Any, key: str, default: Any = None) -> Any:
    """``dict.get`` that tolerates ``None``."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default
