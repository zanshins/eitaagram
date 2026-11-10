"""Client identity and endpoint configuration.

Defaults mirror the Eitaa web client (build 4.6.12, API layer 135) as captured
in the reference sources.  Everything is overridable because the reference
projects disagree on a few wire details (``layer`` 133/135/137, envelope
``flags`` 0/32/128, endpoint path); use :mod:`eitaagram.tools.doctor` or your
own capture to pin the values that work for you.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

CLIENT_ENDPOINTS: tuple[str, ...] = (
    "https://hasan.eitaa.ir/eitaa/",
    "https://hosna.eitaa.com/eitaa/",
    "https://armita.eitaa.com/eitaa/",
    "https://majid.eitaa.com/eitaa/",
    "https://alireza.eitaa.com/eitaa/",
    "https://mostafa.eitaa.com/eitaa/",
    "https://sajad.eitaa.ir/eitaa/",
    "https://bagher.eitaa.ir/eitaa/",
    "https://sadegh.eitaa.ir/eitaa/",
    "https://kazem.eitaa.ir/eitaa/",
)
DOWNLOAD_ENDPOINTS: tuple[str, ...] = (
    "https://mohsen.eitaa.com/eitaa/",
    "https://ghasem.eitaa.com/eitaa/",
    "https://hadi.eitaa.com/eitaa/",
    "https://hossein.eitaa.com/eitaa/",
    "https://vahid.eitaa.com/eitaa/",
)
UPLOAD_ENDPOINTS: tuple[str, ...] = (
    "https://alzheimer.eitaa.com/eitaa/",
    "https://fateme.eitaa.com/eitaa/",
    "https://ali.eitaa.com/eitaa/",
    "https://meysam.eitaa.com/eitaa/",
)

_DEFAULT_UA = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"


@dataclass(frozen=True, slots=True)
class ClientProfile:
    """Static description of the client the server believes it is talking to."""

    api_id: int = 2496
    api_hash: str = "8da85b0d5bfe62527e5b244c209159c3"
    layer: int = 135
    flags: int = 32
    app_version: str = "4.6.12 K"
    build_version: int = 2496
    user_agent: str = _DEFAULT_UA
    system_version: str = "Linux x86_64"
    lang_code: str = "en"
    accept_language: str = "en,de-DE;q=0.9,en-US;q=0.8"
    origin: str = "https://web.eitaa.com"
    client_endpoints: tuple[str, ...] = CLIENT_ENDPOINTS
    upload_endpoints: tuple[str, ...] = UPLOAD_ENDPOINTS
    download_endpoints: tuple[str, ...] = DOWNLOAD_ENDPOINTS
    extra_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.api_id <= 0 or not self.api_hash.strip():
            raise ValueError("api_id / api_hash must be set")
        if self.layer <= 0:
            raise ValueError("layer must be positive")
        if not self.client_endpoints:
            raise ValueError("at least one client endpoint is required")

    def endpoints(self, kind: str) -> tuple[str, ...]:
        if kind == "upload":
            return self.upload_endpoints or self.client_endpoints
        if kind == "download":
            return self.download_endpoints or self.client_endpoints
        return self.client_endpoints

    def headers(self) -> dict[str, str]:
        base = {
            "accept": "*/*",
            "accept-language": self.accept_language,
            "origin": self.origin,
            "user-agent": self.user_agent,
        }
        base.update(self.extra_headers)
        return base

    def app_info(self) -> dict[str, Any]:
        return {
            "_": "eitaaAppInfo",
            "build_version": self.build_version,
            "device_model": self.user_agent,
            "system_version": self.system_version,
            "app_version": self.app_version,
            "lang_code": self.lang_code,
            "sign": "",
        }

    def with_(self, **changes: Any) -> ClientProfile:
        return replace(self, **changes)

    def with_single_endpoint(self, url: str) -> ClientProfile:
        """Pin every kind of traffic to one URL (self-hosted gateway, tests...)."""
        return replace(self, client_endpoints=(url,), upload_endpoints=(url,), download_endpoints=(url,))
