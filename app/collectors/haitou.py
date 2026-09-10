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
from selectolax.parser import HTMLParser, Node

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item
from app.pipeline.gongkao_filter import filter_title_noise_items


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")


def _clean(value: object, limit: int = 300) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(value or "")).split())[:limit]


def _iso_date(value: str) -> str | None:
    match = DATE_RE.search(value)
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else None


def _container(anchor: Node) -> Node:
    node = anchor
    for _ in range(4):
        parent = node.parent
        if parent is None:
            break
        text = _clean(parent.text(separator=" ", strip=True), 2000)
        node = parent
        if any(marker in text for marker in ("地点", "网申", "截止", "招聘岗位")):
            break
    return node


def parse_haitou_html(
    html_text: str, keyword: str, cities: list[str], limit: int,
    *, base_url: str = "https://sx.haitou.cc/",
) -> list[Item]:
    tree = HTMLParser(html_text)
    output: list[Item] = []
    seen: set[str] = set()
    for anchor in tree.css("a[href]"):
        href = urljoin(base_url, str(anchor.attributes.get("href") or ""))
        path = urlparse(href).path.casefold()
        if not any(token in path for token in ("/article/", "/company/", "/job/", "/position/")):
            continue
        node = _container(anchor)
        block = _clean(node.text(separator=" ", strip=True), 1800)
        heading = node.css_first("h1,h2,h3,h4,.company-name,.title")
        title = _clean(heading.text(separator=" ", strip=True) if heading else anchor.text(separator=" ", strip=True), 160)
        if len(title) < 4 or href in seen:
            continue
        city_hits = [name for name in cities if name.casefold() in block.casefold()]
        if cities and not city_hits:
            continue
        company_match = re.search(
            r"([\u4e00-\u9fffA-Za-z0-9（）()·]{2,80}(?:有限公司|股份公司|集团|研究院|研究所|银行|大学))",
            block,
        )
        company = _clean(company_match.group(1) if company_match else title, 120)
        dates = DATE_RE.findall(block)
        date_values = [f"{int(y):04d}-{int(m):02d}-{int(d):02d}" for y, m, d in dates]
        deadline = date_values[-1] if len(date_values) >= 2 else ""
        seen.add(href)
        output.append(Item(
            source="jobs", rank=0, title=title, title_zh=title, url=href,
            summary_zh=_clean(block.replace(title, "", 1), 240),
            published_at=date_values[0] if date_values else None,
            extra={
                "subsource": "haitou", "company": company,
                "city": "、".join(city_hits), "keywords_hit": [keyword],
                "recruitment_type": "校园招聘", "deadline": deadline,
                "is_central_soe": False,
            },
        ))
        if len(output) >= limit:
            break
    return output


class HaitouCollector(BaseCollector):
    source = "jobs"
    timeout = 8.0
    retries = 0

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("HAITOU_CONFIG", "config/haitou.yaml"))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Haitou config missing: {self.config_path}", status="degraded")
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise SourceUnavailable("Haitou config must be a mapping", status="degraded")
        return config

    @staticmethod
    def _timestamp(item: Item) -> float:
        try:
            return datetime.fromisoformat(str(item.published_at or "")).replace(tzinfo=UTC).timestamp()
        except ValueError:
            return 0.0

    async def fetch(self) -> list[Item]:
        config = self.load_config()
        keywords = [str(value).strip() for value in config.get("keywords", []) if str(value).strip()]
        cities = [str(value).strip() for value in config.get("cities", []) if str(value).strip()]
        limit = max(1, int(config.get("per_query_limit", 20)))
        template = str(config.get("list_url") or "https://sx.haitou.cc/article/list?key={keyword}&select_classify=1&select_type=3&type=1")
        if not keywords:
            raise SourceUnavailable("Haitou has no keywords", status="degraded")

        async def query(keyword: str) -> tuple[str, list[Item] | Exception]:
            try:
                response = await self.request(template.format(keyword=quote(keyword)))
                return keyword, parse_haitou_html(response.text, keyword, cities, limit, base_url=str(response.url))
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
            raise SourceUnavailable("Haitou jobs failed or returned no matching rows: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("Haitou partial failure: %s", "; ".join(errors))
        items = sorted(merged.values(), key=lambda item: (-len(item.extra.get("keywords_hit", [])), -self._timestamp(item)))
        items, self.filter_stats, self.filtered_samples = filter_title_noise_items(items)
        for rank, item in enumerate(items, 1):
            item.rank = rank
        return items
