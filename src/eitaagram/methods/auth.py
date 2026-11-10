"""Login flow: ``send_code`` -> ``sign_in`` (-> ``sign_up``), plus a guided ``authorize``."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..errors import (
    ClientError,
    PhoneCodeExpired,
    PhoneCodeHashInvalid,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
    SignUpRequired,
)
from ..types import SentCode, User
from ..utils import normalize_phone

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client

log = logging.getLogger("eitaagram.auth")

CodeCallback = Callable[[SentCode], str | Awaitable[str]]


class AuthMethods:
    async def send_code(self: Client, phone: str, *, allow_flash_call: bool = False) -> SentCode:
        """Ask Eitaa to deliver a login code (SMS / call / in-app, chosen by the server)."""
        number = normalize_phone(phone)
        settings: dict[str, Any] = {"_": "codeSettings"}
        if allow_flash_call:
            settings["allow_flashcall"] = True
        result = await self.invoke(
            "auth.sendCode",
            {
                "phone_number": number,
                "api_id": self.profile.api_id,
                "api_hash": self.profile.api_hash,
                "settings": settings,
            },
            authorized=False,
        )
        return SentCode.from_raw(number, result)

    async def resend_code(self: Client, phone: str, phone_code_hash: str) -> SentCode:
        number = normalize_phone(phone)
        result = await self.invoke(
            "auth.resendCode", {"phone_number": number, "phone_code_hash": phone_code_hash}, authorized=False
        )
        return SentCode.from_raw(number, result)

    async def cancel_code(self: Client, phone: str, phone_code_hash: str) -> bool:
        return bool(
            await self.invoke(
                "auth.cancelCode",
                {"phone_number": normalize_phone(phone), "phone_code_hash": phone_code_hash},
                authorized=False,
            )
        )

    async def sign_in(self: Client, phone: str, phone_code_hash: str, code: str) -> User:
        """Complete the login. Raises :class:`SignUpRequired` for unregistered numbers."""
        number = normalize_phone(phone)
        result = await self.invoke(
            "auth.signIn",
            {"phone_number": number, "phone_code_hash": phone_code_hash, "phone_code": code.strip()},
            authorized=False,
        )
        if result.get("_") == "auth.authorizationSignUpRequired":
            raise SignUpRequired(number, phone_code_hash)
        return await self._accept_authorization(number, result)

    async def sign_up(
        self: Client, phone: str, phone_code_hash: str, code: str, first_name: str, last_name: str = ""
    ) -> User:
        number = normalize_phone(phone)
        result = await self.invoke(
            "auth.signUp",
            {
                "phone_number": number,
                "phone_code_hash": phone_code_hash,
                "phone_code": code.strip(),
                "first_name": first_name.strip(),
                "last_name": last_name.strip(),
                "app_info": self.profile.app_info(),
            },
            authorized=False,
        )
        return await self._accept_authorization(number, result)

    async def _accept_authorization(self: Client, phone: str, result: dict[str, Any]) -> User:
        token = result.get("token")
        if result.get("_") != "auth.authorization" or not token:
            raise ClientError(f"login did not return a token ({result.get('_')})")
        raw_user = result.get("user") or {}
        data = self.session.data
        data.token = str(token)
        data.token_expires_at = None
        data.phone = phone
        data.state = {}  # a fresh login starts a fresh update cursor
        self.unauthorized = False
        if raw_user.get("id") is not None:
            data.user_id = int(raw_user["id"])
            data.username = raw_user.get("username")
            data.is_bot = bool(raw_user.get("bot"))
        self._ingest({"users": [raw_user]})
        await self.session.save()
        self._me = User.from_raw(raw_user) if raw_user.get("id") is not None else None
        await self._emit("authorized", self._me)
        return self._me or User(id=data.user_id or 0)

    async def authorize(
        self: Client,
        phone: str | None = None,
        *,
        code_callback: CodeCallback | None = None,
        first_name: str | None = None,
        last_name: str = "",
        max_code_attempts: int = 3,
    ) -> User:
        """Guided login.

        ``code_callback(sent_code)`` may be sync or async and must return the code
        the user received.  Without it the code is read from stdin (CLI use).
        ``sent_code.type`` tells where Eitaa delivered it (``"sms"``, ``"app"``...).
        """
        phone = phone or self.phone
        if not phone:
            raise ClientError("a phone number is required to log in")
        sent = await self.send_code(phone)
        restarted = False
        attempts = 0
        while True:
            code = await _ask(code_callback, sent)
            try:
                return await self.sign_in(phone, sent.phone_code_hash, code)
            except SignUpRequired:
                if not first_name:
                    raise
                return await self.sign_up(phone, sent.phone_code_hash, code, first_name, last_name)
            except PhoneCodeInvalid:
                attempts += 1
                if attempts >= max_code_attempts:
                    raise
                log.warning("[%s] wrong code, %d attempt(s) left", self.name, max_code_attempts - attempts)
            except (PhoneCodeExpired, PhoneCodeHashInvalid):
                if restarted:
                    raise
                restarted = True
                log.info("[%s] code expired; requesting a new one", self.name)
                sent = await self.send_code(phone)
            except SessionPasswordNeeded:
                raise  # two-step verification is not implemented (see README "Limitations")

    async def log_out(self: Client) -> bool:
        """Terminate this account's authorization on the server and wipe the local session."""
        try:
            ok = bool(await self.invoke("auth.logOut"))
        finally:
            await self.session.delete()
            self._me = None
        return ok

    async def get_me(self: Client) -> User:
        result = await self.invoke("users.getUsers", {"id": [{"_": "inputUserSelf"}]})
        if not result or result[0].get("_") != "user":
            raise ClientError("users.getUsers did not return the current user")
        me = User.from_raw(result[0])
        data = self.session.data
        data.user_id, data.username, data.is_bot = me.id, me.username, me.is_bot
        if me.phone:
            data.phone = me.phone
        self.session.mark_dirty()
        self._me = me
        return me


async def _ask(callback: CodeCallback | None, sent: SentCode) -> str:
    if callback is None:
        where = {"sms": "by SMS", "app": "inside the Eitaa app", "call": "by phone call"}.get(sent.type, sent.type)
        return (await asyncio.to_thread(input, f"Login code for +{sent.phone} (sent {where}): ")).strip()
    result = callback(sent)
    if inspect.isawaitable(result):
        result = await result
    return str(result).strip()
