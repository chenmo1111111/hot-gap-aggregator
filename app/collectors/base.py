from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.models import Item

LOGGER = logging.getLogger(__name__)
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
)


class SourceUnavailable(RuntimeError):
    def __init__(self, message: str, status: str = "skipped") -> None:
        super().__init__(message)
        self.status = status


class BaseCollector(ABC):
    source: str
    timeout = 10.0
    retries = 2

    @abstractmethod
    async def fetch(self) -> list[Item]:
        raise NotImplementedError

    async def request(self, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"User-Agent": random.choice(USER_AGENTS), "Accept": "*/*"}
        headers.update(kwargs.pop("headers", {}))
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.get(url, headers=headers, **kwargs)
                    response.raise_for_status()
                    return response
                except (httpx.TimeoutException, httpx.HTTPError) as exc:
                    last_error = exc
                    if attempt < self.retries:
                        await asyncio.sleep(0.35 * (2**attempt) + random.random() * 0.15)
        raise SourceUnavailable(f"{self.source} request failed: {last_error}", status="degraded")

