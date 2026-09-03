from __future__ import annotations

import asyncio
import html
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import yaml

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item

UTC = timezone.utc


def _plain(value: object) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def _published(value: str) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()
        except ValueError:
            return value


def _timestamp(value: str | None) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


class NowcoderCollector(BaseCollector):
    source = "nowcoder"
    timeout = 15.0

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("NOWCODER_CONFIG", "config/nowcoder.yaml"))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Nowcoder config missing: {self.config_path}", status="degraded")
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise SourceUnavailable("Nowcoder config must be a mapping", status="degraded")
        return raw

    @staticmethod
    def parse(xml_text: str, keywords: list[str]) -> list[Item]:
        root = ET.fromstring(xml_text)
        nodes = root.findall(".//item")
        if not nodes:
            nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].casefold() == "entry"]
        items: list[Item] = []
        for node in nodes:
            values: dict[str, str] = {}
            for child in node:
                key = child.tag.rsplit("}", 1)[-1].casefold()
                text = "".join(child.itertext())
                if key == "link" and not text:
                    text = child.get("href", "")
                values[key] = text
            title = _plain(values.get("title"))
            url = _plain(values.get("link") or values.get("guid") or values.get("id"))
            if not title or not url:
                continue
            description = _plain(values.get("description") or values.get("summary") or values.get("content"))[:300]
            haystack = f"{title} {description}".casefold()
            hits = [keyword for keyword in keywords if keyword.casefold() in haystack]
            published_at = _published(values.get("pubdate") or values.get("published") or values.get("updated") or "")
            items.append(Item(
                source="nowcoder", rank=0, title=title, title_zh=title, url=url,
                hot_value=f"命中 {len(hits)} 个关键词" if hits else None,
                summary_zh=description or None, published_at=published_at,
                extra={"subsource": "rsshub", "keyword_hit": hits, "description": description},
            ))
        return items

    async def fetch(self) -> list[Item]:
        config = self.load_config()
        keywords = [str(value).strip() for value in [*config.get("keywords", []), *config.get("companies", [])] if str(value).strip()]
        base = (os.getenv("RSSHUB_BASE") or str(config.get("rsshub_base") or "https://rsshub.app")).rstrip("/")
        routes = [str(route).strip() for route in config.get("routes", ["/nowcoder/discuss/2"]) if str(route).strip()]
        results = await asyncio.gather(
            *(self.request(f"{base}/{route.lstrip('/')}", headers={"Accept": "application/rss+xml,application/xml"}) for route in routes),
            return_exceptions=True,
        )
        successes = 0
        errors: list[str] = []
        merged: list[Item] = []
        for route, result in zip(routes, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{route}: {result}")
                continue
            try:
                merged.extend(self.parse(result.text, keywords))
                successes += 1
            except ET.ParseError as exc:
                errors.append(f"{route}: invalid RSS: {exc}")
        if successes == 0:
            raise SourceUnavailable("Nowcoder RSSHub failed: " + "; ".join(errors), status="degraded")

        deduplicated: list[Item] = []
        seen: set[str] = set()
        for item in sorted(merged, key=lambda entry: (0 if entry.extra["keyword_hit"] else 1, -_timestamp(entry.published_at))):
            if item.url in seen:
                continue
            seen.add(item.url)
            deduplicated.append(item)
        limit = max(1, int(config.get("limit", 30)))
        output = deduplicated[:limit]
        for rank, item in enumerate(output, 1):
            item.rank = rank
        return output
