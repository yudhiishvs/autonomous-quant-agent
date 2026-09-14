"""Bounded fixed-origin OAuth HTTP transport shared by identity and brokerage boundaries."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from authlib.integrations.requests_client import OAuth2Session
from requests import Response


class ProviderTransportError(Exception):
    """Transport rejected a destination or exceeded a finite response budget."""


class ProviderSession(OAuth2Session):
    """Fixed provider origin, bounded response bodies and finite network deadlines."""

    def __init__(self, *, origin: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.origin = origin
        self.trust_env = False

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        parsed = urlsplit(url)
        if (
            parsed.scheme + "://" + parsed.netloc != self.origin
            or parsed.username
            or parsed.password
        ):
            raise ProviderTransportError("Provider destination rejected.")
        kwargs.update(timeout=(3.05, 10), allow_redirects=False, stream=True)
        started = time.monotonic()
        response: Response = super().request(method, url, **kwargs)
        try:
            content = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                content.extend(chunk)
                if len(content) > 262_144 or time.monotonic() - started > 20:
                    raise ProviderTransportError("Provider response limit exceeded.")
            response._content = bytes(content)
            # Let requests mark its fully buffered content as consumed before closing.
            _ = response.content
            return response
        finally:
            response.close()
