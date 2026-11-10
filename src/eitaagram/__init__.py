"""eitaagram -- async, multi-account Eitaa client library."""

from . import filters
from .client import Client
from .dispatcher import ContinuePropagation, StopPropagation
from .errors import *  # noqa: F401,F403 - the whole exception hierarchy is public API
from .errors import __all__ as _errors_all
from .pool import Account, AccountPool, AccountState
from .profile import ClientProfile
from .ratelimit import RateLimiter
from .session import (
    FernetCipher,
    FileSession,
    MemorySession,
    Session,
    SQLiteSession,
    StringSession,
)
from .types import Chat, DeletedMessages, Dialog, Message, SentCode, User

__version__ = "0.1.0"

__all__ = [
    "Client",
    "AccountPool",
    "Account",
    "AccountState",
    "ClientProfile",
    "RateLimiter",
    "Session",
    "MemorySession",
    "StringSession",
    "FileSession",
    "SQLiteSession",
    "FernetCipher",
    "User",
    "Chat",
    "Message",
    "Dialog",
    "SentCode",
    "DeletedMessages",
    "filters",
    "StopPropagation",
    "ContinuePropagation",
    *_errors_all,
]
