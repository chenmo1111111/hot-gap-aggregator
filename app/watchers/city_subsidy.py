"""Backward-compatible adapter for the renamed subsidy watcher."""

import os
from pathlib import Path
from typing import Any

from app.store.database import Database

from .subsidy_watch import Judge, SubsidyWatcher, extract_main_text


class CitySubsidyWatcher(SubsidyWatcher):
    def __init__(
        self, database: Database, config_path: str | Path | None = None, *,
        judge: Judge | None = None, notifier: Any = None,
    ) -> None:
        async def legacy_notifier(alert: dict[str, str]) -> dict[str, str]:
            message = alert["message"]
            if alert.get("type") == "政策变动":
                message = message.replace(f"【补贴预警·{alert['region']}】{alert['title']}", f"【补贴变动】{alert['region']}·{alert['title']}", 1)
            return await notifier(message, "城市人才补贴变动")

        path = config_path or os.getenv("CITY_SUBSIDY_CONFIG", "config/city_subsidy.yaml")
        super().__init__(database, path, judge=judge, notifier=legacy_notifier if notifier else None)

    async def _fetch(self, url: str):
        return await super()._fetch_response(url)

    async def _fetch_response(self, url: str):
        return await self._fetch(url)

    async def _deliver(self, alert: dict[str, str]) -> bool:
        statuses = await self.notifier(alert)
        return any(status == "ok" for status in statuses.values())

    async def run(self) -> list[dict[str, str]]:
        result = await super().run()
        return result["policy_pages"]

__all__ = ["CitySubsidyWatcher", "extract_main_text"]
