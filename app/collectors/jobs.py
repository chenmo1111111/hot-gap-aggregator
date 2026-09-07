from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from app.collectors.base import BaseCollector, SourceUnavailable
from app.collectors.guopin import GuopinCollector
from app.collectors.job_radar import JobRadarCollector
from app.collectors.yingjiesheng import YingjieshengCollector
from app.models import Item


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


class JobsCollector(BaseCollector):
    """Combine independent job providers before the shared ``jobs`` DB write."""

    source = "jobs"

    def __init__(self, providers: list[BaseCollector] | None = None) -> None:
        if providers is not None:
            self.providers = providers
            return
        # When the source is assigned to the mainland runner, Actions must not
        # open a browser for it. The server-owned sidecar is merged by the UI.
        self.providers = [JobRadarCollector()]
        if not _enabled(os.getenv("YINGJIESHENG_ON_SERVER")):
            self.providers.append(YingjieshengCollector())
        self.providers.append(GuopinCollector())

    @staticmethod
    def _timestamp(item: Item) -> float:
        try:
            value = datetime.fromisoformat(str(item.published_at or "").replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()
        except ValueError:
            return 0.0

    async def fetch(self) -> list[Item]:
        results = await asyncio.gather(*(provider.fetch() for provider in self.providers), return_exceptions=True)
        merged: list[Item] = []
        errors: list[str] = []
        for provider, result in zip(self.providers, results, strict=True):
            name = provider.__class__.__name__.removesuffix("Collector")
            if isinstance(result, BaseException):
                errors.append(f"{name}: {result}")
            else:
                merged.extend(result)
        if len(errors) == len(self.providers):
            raise SourceUnavailable("All jobs providers failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("jobs partial failure: %s", "; ".join(errors))
        unique: dict[tuple[str, str], Item] = {}
        for item in merged:
            key = (item.title.casefold(), str(item.extra.get("company") or "").casefold())
            if key in unique:
                old = unique[key]
                old.extra["keywords_hit"] = list(dict.fromkeys([*old.extra.get("keywords_hit", []), *item.extra.get("keywords_hit", [])]))
            else:
                unique[key] = item
        output = sorted(unique.values(), key=lambda item: (-len(item.extra.get("keywords_hit", [])), -self._timestamp(item)))
        for rank, item in enumerate(output, 1):
            item.rank = rank
        return output
