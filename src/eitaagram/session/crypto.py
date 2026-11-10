"""Optional at-rest encryption for stored sessions (needs ``cryptography``)."""

from __future__ import annotations


class FernetCipher:
    """Authenticated symmetric encryption (AES-128-CBC + HMAC) via ``cryptography``."""

    def __init__(self, key: bytes | str) -> None:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover
            raise ImportError("FernetCipher needs: pip install cryptography") from exc
        self._fernet = Fernet(key)

    @staticmethod
    def generate_key() -> bytes:
        from cryptography.fernet import Fernet

        return bytes(Fernet.generate_key())

    def encrypt(self, data: bytes) -> bytes:
        return bytes(self._fernet.encrypt(data))

    def decrypt(self, data: bytes) -> bytes:
        return bytes(self._fernet.decrypt(data))
