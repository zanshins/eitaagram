"""Exception hierarchy.

Everything raised by the library derives from :class:`EitaaError`.  Server-side
errors are :class:`RPCError` subclasses chosen from the error text, so callers
can write ``except FloodWait as e: await asyncio.sleep(e.value)`` exactly like
in Telethon / Pyrogram.
"""

from __future__ import annotations

import re
from typing import ClassVar

__all__ = [
    "EitaaError",
    "TransportError",
    "TLError",
    "RPCError",
    "BadRequest",
    "Unauthorized",
    "Forbidden",
    "NotFound",
    "FloodWait",
    "ServerError",
    "SessionExpired",
    "TokenExpired",
    "TokenUpdating",
    "SessionRevoked",
    "PhoneNumberInvalid",
    "PhoneNumberBanned",
    "PhoneNumberFlood",
    "PhoneCodeInvalid",
    "PhoneCodeExpired",
    "PhoneCodeHashInvalid",
    "SessionPasswordNeeded",
    "UsernameInvalid",
    "UsernameOccupied",
    "UsernameNotOccupied",
    "PeerIdInvalid",
    "ChannelPrivate",
    "ChatWriteForbidden",
    "UserPrivacyRestricted",
    "InviteHashExpired",
    "InviteHashInvalid",
    "UserAlreadyParticipant",
    "MessageIdInvalid",
    "InvalidConstructor",
    "ClientError",
    "NotAuthorized",
    "SignUpRequired",
    "PeerNotFound",
    "MethodNotSupported",
    "PoolError",
    "AccountNotFound",
    "AccountUnavailable",
    "parse_wait_seconds",
]

_WAIT_RE = re.compile(
    r"(?:FLOOD(?:_PREMIUM)?_WAIT|SLOWMODE_WAIT|RETRY_AFTER|WAIT)_(?P<s>\d+)(?:\D|$)",
    re.IGNORECASE,
)
_PROSE_WAIT_RE = re.compile(
    r"(?:retry\s+after|try\s+again\s+in|wait)\D{0,24}(?P<s>\d+)\s*(?:seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_TRAILING_NUMBER_RE = re.compile(r"_(\d+)$")


def parse_wait_seconds(text: str) -> int | None:
    """Return the exact delay announced by the server, or ``None``.

    Only explicit forms such as ``FLOOD_WAIT_120`` or "try again in 120 seconds"
    are accepted; unrelated digits (``RETRY_LIMIT404``) are ignored.
    """
    for pattern in (_WAIT_RE, _PROSE_WAIT_RE):
        match = pattern.search(text)
        if match:
            return int(match.group("s"))
    return None


class EitaaError(Exception):
    """Base class for every error raised by eitaagram."""

    retry_after: int | None = None


class TransportError(EitaaError):
    """Every endpoint failed (network error, timeout, HTTP error, empty body)."""

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TLError(EitaaError):
    """Invalid TL input or a malformed / unknown TL response."""


class RPCError(EitaaError):
    """An error object returned by the Eitaa server."""

    CODE: ClassVar[int | None] = None
    ID: ClassVar[str | None] = None

    def __init__(self, code: int, text: str, method: str | None = None) -> None:
        self.code = code
        self.text = text
        self.method = method
        self.id = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")
        trailing = _TRAILING_NUMBER_RE.search(self.id)
        #: Numeric suffix of identifiers such as ``FLOOD_WAIT_30`` (30).
        self.value: int | None = int(trailing.group(1)) if trailing else None
        self.retry_after = parse_wait_seconds(text)
        prefix = f"{method}: " if method else ""
        super().__init__(f"{prefix}{text} (code {code})")

    @property
    def retryable(self) -> bool:
        return bool(
            self.retry_after is not None
            or self.code in {420, 429, 500, 502, 503, 504}
            or self.id.startswith(("FLOOD_", "RETRY_", "TEMPORARILY_", "TIMEOUT", "SERVER_"))
        )

    @classmethod
    def build(cls, code: int, text: str, method: str | None = None) -> RPCError:
        """Pick the most specific subclass for ``(code, text)``."""
        ident = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")
        base = _TRAILING_NUMBER_RE.sub("", ident)
        for key in (ident, base):
            klass = _BY_ID.get(key)
            if klass is not None:
                return klass(code, text, method)
        if parse_wait_seconds(text) is not None and "FLOOD" in ident:
            return FloodWait(code, text, method)
        klass = _BY_CODE.get(code)
        if klass is not None:
            return klass(code, text, method)
        if 500 <= code < 600:
            return ServerError(code, text, method)
        return RPCError(code, text, method)


class BadRequest(RPCError):
    CODE = 400


class Unauthorized(RPCError):
    CODE = 401


class Forbidden(RPCError):
    CODE = 403


class NotFound(RPCError):
    CODE = 404


class FloodWait(RPCError):
    """The server asks the caller to wait ``value`` seconds."""

    CODE = 420

    @property
    def wait(self) -> int:
        return self.retry_after if self.retry_after is not None else (self.value or 1)


class ServerError(RPCError):
    CODE = 500


class SessionExpired(Unauthorized):
    """The token is no longer accepted; a refresh (or re-login) is required."""


class TokenExpired(SessionExpired):
    """Eitaa returned the ``eitaa_updates_expire_token`` marker."""


class TokenUpdating(SessionExpired):
    """Eitaa returned the ``eitaa_token_updating`` marker."""


class SessionRevoked(SessionExpired):
    """The authorization was terminated (logout from another device, ban...)."""


class PhoneNumberInvalid(BadRequest):
    ID = "PHONE_NUMBER_INVALID"


class PhoneNumberBanned(BadRequest):
    ID = "PHONE_NUMBER_BANNED"


class PhoneNumberFlood(BadRequest):
    """Too many login attempts for this number; not a timed wait."""

    ID = "PHONE_NUMBER_FLOOD"


class PhoneCodeInvalid(BadRequest):
    ID = "PHONE_CODE_INVALID"


class PhoneCodeExpired(BadRequest):
    ID = "PHONE_CODE_EXPIRED"


class PhoneCodeHashInvalid(BadRequest):
    ID = "PHONE_CODE_HASH_EMPTY"


class SessionPasswordNeeded(Unauthorized):
    ID = "SESSION_PASSWORD_NEEDED"


class UsernameInvalid(BadRequest):
    ID = "USERNAME_INVALID"


class UsernameOccupied(BadRequest):
    ID = "USERNAME_OCCUPIED"


class UsernameNotOccupied(BadRequest):
    ID = "USERNAME_NOT_OCCUPIED"


class PeerIdInvalid(BadRequest):
    ID = "PEER_ID_INVALID"


class ChannelPrivate(BadRequest):
    ID = "CHANNEL_PRIVATE"


class ChatWriteForbidden(Forbidden):
    ID = "CHAT_WRITE_FORBIDDEN"


class UserPrivacyRestricted(Forbidden):
    ID = "USER_PRIVACY_RESTRICTED"


class InviteHashExpired(BadRequest):
    ID = "INVITE_HASH_EXPIRED"


class InviteHashInvalid(BadRequest):
    ID = "INVITE_HASH_INVALID"


class UserAlreadyParticipant(BadRequest):
    ID = "USER_ALREADY_PARTICIPANT"


class MessageIdInvalid(BadRequest):
    ID = "MESSAGE_ID_INVALID"


class InvalidConstructor(BadRequest):
    ID = "INVALID_CONSTRUCTOR"


_BY_CODE: dict[int, type[RPCError]] = {
    400: BadRequest,
    401: Unauthorized,
    403: Forbidden,
    404: NotFound,
    420: FloodWait,
    429: FloodWait,
}

_BY_ID: dict[str, type[RPCError]] = {}


def _register(klass: type[RPCError], *ids: str) -> None:
    for ident in ids:
        _BY_ID[ident] = klass


for _klass in (
    PhoneNumberInvalid,
    PhoneNumberBanned,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded,
    UsernameInvalid,
    UsernameOccupied,
    UsernameNotOccupied,
    PeerIdInvalid,
    ChannelPrivate,
    ChatWriteForbidden,
    UserPrivacyRestricted,
    InviteHashExpired,
    InviteHashInvalid,
    UserAlreadyParticipant,
    MessageIdInvalid,
    InvalidConstructor,
):
    if _klass.ID:
        _register(_klass, _klass.ID)

_register(PhoneNumberInvalid, "PHONE_NUMBER_EMPTY")
_register(PhoneCodeInvalid, "PHONE_CODE_EMPTY", "CODE_INVALID")
_register(PhoneCodeExpired, "CODE_EXPIRED")
_register(
    PhoneCodeHashInvalid,
    "PHONE_CODE_HASH_EMPTY",
    "PHONE_CODE_HASH_INVALID",
    "PHONE_CODE_HASH_EXPIRED",
)
_register(SessionPasswordNeeded, "PASSWORD_HASH_INVALID")
_register(FloodWait, "FLOOD_WAIT", "FLOOD_PREMIUM_WAIT", "SLOWMODE_WAIT")
_register(PhoneNumberFlood, "PHONE_NUMBER_FLOOD")
_register(
    SessionExpired,
    "INVALID_LOGIN",
    "AUTH_KEY_INVALID",
    "SESSION_EXPIRED",
)
_register(
    SessionRevoked,
    "SESSION_REVOKED",
    "AUTH_KEY_UNREGISTERED",
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
)
_register(TokenExpired, "EITAA_UPDATES_EXPIRE_TOKEN")
_register(TokenUpdating, "EITAA_TOKEN_UPDATING")


# --------------------------------------------------------------------------- #
# Client-side errors (never sent by the server)
# --------------------------------------------------------------------------- #
class ClientError(EitaaError):
    """Misuse or local state problem."""


class NotAuthorized(ClientError):
    """The operation needs a logged-in account."""


class SignUpRequired(ClientError):
    """The phone number is not registered; call ``sign_up`` instead."""

    def __init__(self, phone: str, phone_code_hash: str) -> None:
        super().__init__(f"{phone} is not registered on Eitaa; sign_up() is required")
        self.phone = phone
        self.phone_code_hash = phone_code_hash


class PeerNotFound(ClientError):
    """A user / chat / channel reference could not be resolved."""


class MethodNotSupported(ClientError):
    """The server rejected the TL constructor (``INVALID_CONSTRUCTOR``)."""


class PoolError(ClientError):
    """Base for :class:`AccountPool` errors."""


class AccountNotFound(PoolError):
    pass


class AccountUnavailable(PoolError):
    """No account in a usable state matches the request."""
