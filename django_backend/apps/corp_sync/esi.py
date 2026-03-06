from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings


class CorporationEsiClient:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(base_url=settings.EVE_API_BASE, timeout=60.0)

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, token: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._client.get(
            path,
            params=params,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )


def parse_x_pages(response: httpx.Response) -> int:
    raw = response.headers.get("x-pages")
    try:
        return int(raw) if raw else 1
    except (TypeError, ValueError):
        return 1
