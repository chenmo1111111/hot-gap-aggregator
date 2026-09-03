from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import struct_time
from typing import Any

import feedparser
import yaml

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item

UTC = timezone.utc
LOGGER = logging.getLogger(__name__)
ALLOWED_TABS = {"hot", "ai", "papers", "tools", "jobs"}


def _plain(value: object) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def _entry_description(entry: Any) -> str:
    value = entry.get("summary") or entry.get("description") or ""
    if not value:
        content = entry.get("content") or []
        if content and isinstance(content[0], dict):
            value = content[0].get("value") or ""
    return _plain(value)[:400]


def _iso_date(entry: Any) -> str | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=UTC).isoformat()
    raw = str(entry.get("published") or entry.get("updated") or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()
        except ValueError:
            return raw


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class FeedsCollector(BaseCollector):
    """Consume configurable RSSHub routes without coupling them to collectors."""

    source = "feed"
    timeout = 20.0

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("FEEDS_CONFIG", "config/feeds.yaml"))

    def load_config(self) -> list[dict[str, Any]]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Feeds config missing: {self.config_path}", status="skipped")
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        rows = payload.get("feeds", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise SourceUnavailable("feeds.yaml: feeds must be a list", status="degraded")
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def parse(content: bytes | str, feed: dict[str, Any]) -> list[Item]:
        parsed = feedparser.parse(content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"invalid feed: {parsed.bozo_exception}")
        name = str(feed.get("name") or "RSSHub").strip()
        route = str(feed.get("route") or feed.get("url") or "").strip()
        tab = str(feed.get("tab") or "hot").strip().casefold()
        if tab not in ALLOWED_TABS:
            raise ValueError(f"unsupported feed tab: {tab}")
        translate = bool(feed.get("translate", False))
        limit = max(1, int(feed.get("limit", 20)))
        items: list[Item] = []
        for entry in parsed.entries[:limit]:
            title = _plain(entry.get("title"))
            url = str(entry.get("link") or entry.get("id") or "").strip()
            if not title or not url:
                continue
            description = _entry_description(entry)
            items.append(Item(
                source="feed", rank=0, title=title, title_zh=title, url=url,
                summary_zh=description or None, published_at=_iso_date(entry),
                extra={
                    "feed_name": name, "tab": tab, "route": route,
                    "translate": translate, "description": description,
                    "dedupe_key": url,
                },
            ))
        return items

    @staticmethod
    def group_by_tab(items: list[Item]) -> dict[str, list[Item]]:
        grouped = {tab: [] for tab in ALLOWED_TABS}
        for item in items:
            tab = str(item.extra.get("tab") or "hot")
            if tab in grouped:
                grouped[tab].append(item)
        return grouped

    @staticmethod
    def _safe_error(error: BaseException, key: str) -> str:
        message = str(error)
        return message.replace(key, "[redacted]") if key else message

    async def fetch(self) -> list[Item]:
        base = os.getenv("RSSHUB_BASE", "").strip().rstrip("/")
        key = os.getenv("RSSHUB_KEY", "").strip()
        feeds = self.load_config()
        valid: list[dict[str, Any]] = []
        for feed in feeds:
            direct_url = str(feed.get("url") or "").strip()
            route = str(feed.get("route") or "").strip()
            configured = direct_url or route
            if not configured or "<待填" in configured:
                LOGGER.warning("feed %s skipped: route is not configured", feed.get("name") or route)
                continue
            if direct_url and not direct_url.startswith(("https://", "http://")):
                LOGGER.warning("feed %s skipped: direct URL must be HTTP(S)", feed.get("name") or direct_url)
                continue
            if route and not direct_url and not base:
                LOGGER.warning("feed %s skipped: RSSHUB_BASE is not configured", feed.get("name") or route)
                continue
            valid.append(feed)
        if not valid:
            raise SourceUnavailable("No configured RSSHub or direct feed routes", status="skipped")

        async def one(feed: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
            direct_url = str(feed.get("url") or "").strip()
            route = str(feed.get("route") or "").strip()
            target = direct_url or f"{base}/{route.lstrip('/')}"
            request_kwargs: dict[str, Any] = {
                "headers": {"Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml"},
            }
            if not direct_url:
                params: dict[str, object] = {"limit": max(1, int(feed.get("limit", 20)))}
                if key:
                    params["key"] = key
                request_kwargs["params"] = params
            response = await self.request(
                target, **request_kwargs,
            )
            return feed, response.content

        results = await asyncio.gather(*(one(feed) for feed in valid), return_exceptions=True)
        merged: list[Item] = []
        errors: list[str] = []
        successes = 0
        for feed, result in zip(valid, results, strict=True):
            name = str(feed.get("name") or feed.get("route") or "feed")
            if isinstance(result, BaseException):
                errors.append(f"{name}: {self._safe_error(result, key)}")
                continue
            try:
                _, content = result
                items = self.parse(content, feed)
                if not items:
                    raise ValueError("empty feed")
                merged.extend(items)
                successes += 1
            except Exception as exc:  # feedparser isolates malformed routes
                errors.append(f"{name}: {self._safe_error(exc, key)}")
        if successes == 0:
            raise SourceUnavailable("All RSSHub feeds failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("RSSHub feeds partial failure: %s", "; ".join(errors))

        deduplicated: list[Item] = []
        seen: set[str] = set()
        for item in sorted(merged, key=lambda entry: -_timestamp(entry.published_at)):
            key_value = item.url.strip().casefold()
            if key_value in seen:
                continue
            seen.add(key_value)
            deduplicated.append(item)
        for rank, item in enumerate(deduplicated, 1):
            item.rank = rank
        return deduplicated
