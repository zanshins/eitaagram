from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..types import User

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client


class AccountMethods:
    async def update_profile(
        self: Client, *, first_name: str | None = None, last_name: str | None = None, about: str | None = None
    ) -> User:
        params: dict[str, Any] = {"first_name": first_name, "last_name": last_name, "about": about}
        result = await self.invoke("account.updateProfile", {k: v for k, v in params.items() if v is not None})
        self._me = User.from_raw(result)
        return self._me

    async def set_username(self: Client, username: str) -> User:
        result = await self.invoke("account.updateUsername", {"username": username.lstrip("@")})
        self._me = User.from_raw(result)
        self.session.data.username = self._me.username
        self.session.mark_dirty()
        return self._me

    async def check_username(self: Client, username: str) -> bool:
        return bool(await self.invoke("account.checkUsername", {"username": username.lstrip("@")}))

    async def set_online(self: Client, online: bool = True) -> bool:
        return bool(await self.invoke("account.updateStatus", {"offline": not online}))

    async def get_sessions(self: Client) -> list[dict[str, Any]]:
        """Active authorizations of this account (raw TL dicts)."""
        result = await self.invoke("account.getAuthorizations")
        return list(result.get("authorizations", []))
