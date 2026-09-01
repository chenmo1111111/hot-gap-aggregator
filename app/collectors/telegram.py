from __future__ import annotations

import asyncio
import os
from pathlib import Path

import yaml
from selectolax.parser import HTMLParser

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item


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
    def parse_channel(html: str, channel: str, translate: bool = True) -> list[Item]:
        tree = HTMLParser(html)
        items: list[Item] = []
        for wrap in tree.css(".tgme_widget_message_wrap")[-10:]:
            text_node = wrap.css_first(".tgme_widget_message_text")
            date_link = wrap.css_first(".tgme_widget_message_date")
            time_node = wrap.css_first(".tgme_widget_message_date time")
            if not text_node or not date_link or not date_link.attributes.get("href"):
                continue
            title = " ".join(text_node.text(separator=" ", strip=True).split())[:300]
            if not title:
                continue
            views = wrap.css_first(".tgme_widget_message_views")
            items.append(Item(
                source="telegram", rank=len(items) + 1, title=title, title_zh=title,
                url=date_link.attributes["href"], hot_value=views.text(strip=True) if views else None,
                published_at=time_node.attributes.get("datetime") if time_node else None,
                extra={"channel": channel, "translate": translate},
            ))
        return items

    async def _fetch_channel(self, channel: str, translate: bool) -> list[Item]:
        response = await self.request(f"https://t.me/s/{channel}", headers={"Accept": "text/html"})
        return self.parse_channel(response.text, channel, translate)

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
            raise SourceUnavailable("; ".join(errors) or "Telegram returned no messages", status="degraded")
        for index, item in enumerate(items, 1):
            item.rank = index
        return items
