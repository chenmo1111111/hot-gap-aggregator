from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from selectolax.parser import HTMLParser

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item

MAX_AGE_DAYS = 5
PER_CHANNEL_LIMIT = 6


class TelegramCollector(BaseCollector):
    source = "telegram"

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("TELEGRAM_CHANNELS_CONFIG", "config/telegram_channels.yaml"))

    def load_config(self) -> tuple[list[str], bool]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Telegram config missing: {self.config_path}", status="degraded")
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            channels, translate = data, True
        elif isinstance(data, dict):
            channels, translate = data.get("channels") or [], bool(data.get("translate_telegram", True))
        else:
            channels, translate = [], True
        clean = [str(channel).strip().lstrip("@") for channel in channels if str(channel).strip()]
        if not clean:
            raise SourceUnavailable("Telegram channel list is empty", status="degraded")
        return clean, translate

    @staticmethod
    def parse_channel(html: str, channel: str, translate: bool = True, *, max_age_days: int = MAX_AGE_DAYS) -> list[Item]:
        tree = HTMLParser(html)
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        items: list[Item] = []
        for wrap in tree.css(".tgme_widget_message_wrap")[-20:]:
            text_node = wrap.css_first(".tgme_widget_message_text")
            date_link = wrap.css_first(".tgme_widget_message_date")
            time_node = wrap.css_first(".tgme_widget_message_date time")
            if not text_node or not date_link or not date_link.attributes.get("href"):
                continue
            published_at = time_node.attributes.get("datetime") if time_node else None
            posted = _parse_dt(published_at)
            if posted is None or posted < cutoff:
                continue  # drop old pinned / stale announcement posts
            title = " ".join(text_node.text(separator=" ", strip=True).split())[:300]
            if not title:
                continue
            views = wrap.css_first(".tgme_widget_message_views")
            items.append(Item(
                source="telegram", rank=len(items) + 1, title=title, title_zh=title,
                url=date_link.attributes["href"], hot_value=views.text(strip=True) if views else None,
                published_at=published_at,
                extra={"channel": channel, "translate": translate},
            ))
        return items[-PER_CHANNEL_LIMIT:]

    async def _fetch_channel(self, channel: str, translate: bool) -> list[Item]:
        response = await self.request(f"https://t.me/s/{channel}", headers={"Accept": "text/html"})
        items = self.parse_channel(response.text, channel, translate)
        if not items:  # channel posted nothing recent - keep only its single latest
            items = self.parse_channel(response.text, channel, translate, max_age_days=36500)[-1:]
        return items

    async def fetch(self) -> list[Item]:
        channels, translate = self.load_config()
        results = await asyncio.gather(
            *(self._fetch_channel(channel, translate) for channel in channels), return_exceptions=True,
        )
        items: list[Item] = []
        errors: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(str(result))
            else:
                items.extend(result)
        if not items:
            raise SourceUnavailable("; ".join(errors) or "Telegram returned no recent messages", status="degraded")
        items.sort(key=lambda item: item.published_at or "", reverse=True)
        for index, item in enumerate(items, 1):
            item.rank = index
        return items


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
