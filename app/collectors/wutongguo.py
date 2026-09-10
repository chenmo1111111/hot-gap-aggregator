from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import yaml
from selectolax.parser import HTMLParser

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item
from app.pipeline.gongkao_filter import filter_title_noise_items


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")


def _clean(value: object, limit: int = 300) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(value or "")).split())[:limit]


def parse_wutongguo_html(
    html_text: str, keyword: str, cities: list[str], limit: int,
    *, base_url: str = "https://www.wutongguo.com/",
) -> list[Item]:
    tree = HTMLParser(html_text)
    output: list[Item] = []
    seen: set[str] = set()
    for anchor in tree.css("a[href]"):
        href = urljoin(base_url, str(anchor.attributes.get("href") or ""))
        if not any(token in urlparse(href).path.casefold() for token in ("/job/", "/position/", "/campus/", "/company/")):
            continue
        node = anchor
        for _ in range(3):
            if node.parent is None:
                break
            node = node.parent
            classes = str(node.attributes.get("class") or "").casefold()
            if node.tag in {"li", "article"} or any(
                token in classes for token in ("job-card", "job-item", "position-item", "campus-item")
            ):
                break
        block = _clean(node.text(separator=" ", strip=True), 1500)
        heading = node.css_first("h1,h2,h3,h4,.title,.job-name")
        title = _clean(heading.text(separator=" ", strip=True) if heading else anchor.text(separator=" ", strip=True), 160)
        if len(title) < 4 or href in seen:
            continue
        city_hits = [name for name in cities if name.casefold() in block.casefold()]
        if cities and not city_hits:
            continue
        company_node = node.css_first(".company,.company-name,[class*=company]")
        company = _clean(company_node.text(separator=" ", strip=True) if company_node else "梧桐果", 120)
        dates = DATE_RE.findall(block)
        values = [f"{int(y):04d}-{int(m):02d}-{int(d):02d}" for y, m, d in dates]
        seen.add(href)
        output.append(Item(
            source="jobs", rank=0, title=title, title_zh=title, url=href,
            summary_zh=_clean(block.replace(title, "", 1), 240),
            published_at=values[0] if values else None,
            extra={
                "subsource": "wutongguo", "company": company,
                "city": "、".join(city_hits), "keywords_hit": [keyword],
                "recruitment_type": "校园招聘",
                "deadline": values[-1] if len(values) >= 2 else "",
                "is_central_soe": False,
            },
        ))
        if len(output) >= limit:
            break
    return output


class WutongguoCollector(BaseCollector):
    source = "jobs"
    timeout = 8.0
    retries = 0

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("WUTONGGUO_CONFIG", "config/wutongguo.yaml"))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Wutongguo config missing: {self.config_path}", status="degraded")
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise SourceUnavailable("Wutongguo config must be a mapping", status="degraded")
        return config

    async def fetch(self) -> list[Item]:
        config = self.load_config()
        keywords = [str(value).strip() for value in config.get("keywords", []) if str(value).strip()]
        cities = [str(value).strip() for value in config.get("cities", []) if str(value).strip()]
        limit = max(1, int(config.get("per_query_limit", 20)))
        template = str(config.get("list_url") or "https://www.wutongguo.com/search?keyword={keyword}")
        if not keywords:
            raise SourceUnavailable("Wutongguo has no keywords", status="degraded")

        async def query(keyword: str) -> tuple[str, list[Item] | Exception]:
            try:
                response = await self.request(template.format(keyword=quote(keyword)))
                return keyword, parse_wutongguo_html(response.text, keyword, cities, limit, base_url=str(response.url))
            except Exception as exc:
                return keyword, exc

        results = await asyncio.gather(*(query(keyword) for keyword in keywords))
        merged: dict[tuple[str, str], Item] = {}
        errors: list[str] = []
        for keyword, result in results:
            if isinstance(result, Exception):
                errors.append(f"{keyword}: {result}")
                continue
            for item in result:
                key = (item.title.casefold(), str(item.extra.get("company") or "").casefold())
                if key in merged:
                    merged[key].extra["keywords_hit"] = list(dict.fromkeys([
                        *merged[key].extra.get("keywords_hit", []), keyword,
                    ]))
                else:
                    merged[key] = item
        if not merged:
            raise SourceUnavailable("Wutongguo jobs failed or returned no matching rows: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("Wutongguo partial failure: %s", "; ".join(errors))
        items = sorted(merged.values(), key=lambda item: (
            -len(item.extra.get("keywords_hit", [])),
            -(datetime.fromisoformat(str(item.published_at)).replace(tzinfo=UTC).timestamp() if item.published_at else 0),
        ))
        items, self.filter_stats, self.filtered_samples = filter_title_noise_items(items)
        for rank, item in enumerate(items, 1):
            item.rank = rank
        return items
