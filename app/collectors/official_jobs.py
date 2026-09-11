"""Lightweight collectors for official autumn-recruitment platforms."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml
from selectolax.parser import HTMLParser

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item
from app.pipeline.gongkao_filter import filter_title_noise_items


LOGGER = logging.getLogger(__name__)
CHINA_TZ = timezone(timedelta(hours=8))
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")
COMPANY_RE = re.compile(
    r"^(.{2,80}?(?:大学|学院|研究院|研究所|实验室|中心|学会|公司|集团|学校))"
    r"(?=20\d{2}|招聘|公开招聘|博士后|专职|$)"
)


def _clean(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _path(value: object, dotted: str) -> object:
    current = value
    for part in dotted.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current


def _date_value(value: object) -> date | None:
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, CHINA_TZ).date()
        except (OSError, OverflowError, ValueError):
            return None
    match = DATE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _company_from_title(title: str) -> str:
    match = COMPANY_RE.search(title)
    return _clean(match.group(1) if match else title, 120)


class OfficialJobsCollector(BaseCollector):
    """Collect NCSS job JSON and CHSI recruitment announcement lists."""

    source = "jobs"
    timeout = 20.0

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(
            config_path
            or os.getenv("OFFICIAL_JOB_SOURCES_CONFIG", "config/official_job_sources.yaml")
        )
        self.stats: dict[str, dict[str, int | str]] = {}
        self.failed_subsources: set[str] = set()
        self.filter_stats: dict[str, int] = {}
        self.filtered_samples: list[dict[str, str]] = []

    def load_sources(self) -> list[dict[str, Any]]:
        if not self.config_path.exists():
            raise SourceUnavailable(
                f"Official job sources config missing: {self.config_path}", status="degraded"
            )
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        rows = payload.get("sources") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise SourceUnavailable("Official job sources must contain a sources list", status="degraded")
        return [
            dict(row) for row in rows
            if isinstance(row, Mapping) and row.get("enabled", True) and row.get("list_url")
        ]

    @staticmethod
    def parse_api(
        payload: object, source: Mapping[str, Any], *, keyword: str = "",
        today: date | None = None,
    ) -> list[Item]:
        current = today or datetime.now(CHINA_TZ).date()
        cutoff = current - timedelta(days=max(1, int(source.get("recent_days") or 45)))
        rows = _path(payload, str(source.get("items_path") or "data.list"))
        if not isinstance(rows, list):
            return []
        fields = source.get("fields") if isinstance(source.get("fields"), Mapping) else {}
        output: list[Item] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            title = _clean(row.get(str(fields.get("title") or "jobName")), 180)
            company = _clean(row.get(str(fields.get("company") or "recName")), 120)
            item_id = _clean(row.get(str(fields.get("id") or "jobId")), 100)
            published = _date_value(row.get(str(fields.get("date") or "publishDate")))
            if not title or not company or not item_id or not published:
                continue
            if not cutoff <= published <= current + timedelta(days=1):
                continue
            link_template = str(
                source.get("link_template")
                or "https://cg.ncss.cn/student/jobs/{id}/detail.html"
            )
            url = link_template.format(id=item_id)
            direct_hits = [
                str(value) for value in source.get("keywords", [])
                if str(value) and str(value).casefold() in f"{title} {company}".casefold()
            ]
            hits = list(dict.fromkeys([keyword, *direct_hits])) if keyword else direct_hits
            company_type = _clean(row.get(str(fields.get("company_type") or "recProperty")), 80)
            output.append(Item(
                source="jobs", rank=len(output) + 1, title=title, title_zh=title,
                url=url, published_at=published.isoformat(),
                extra={
                    "subsource": str(source.get("subsource") or "ncss"),
                    "source_label": str(source.get("name") or "国家大学生就业服务平台"),
                    "company": company,
                    "company_type": company_type,
                    "city": _clean(row.get(str(fields.get("city") or "areaCodeName")), 80),
                    "education": _clean(row.get(str(fields.get("education") or "degreeName")), 80),
                    "keywords_hit": [value for value in hits if value],
                    "recruitment_type": "校园招聘",
                    "is_central_soe": company_type == "国有企业",
                    "official_platform": True,
                    "official_job_id": item_id,
                },
            ))
        return output

    @staticmethod
    def parse_html(
        document: str, source: Mapping[str, Any], *, today: date | None = None,
    ) -> list[Item]:
        current = today or datetime.now(CHINA_TZ).date()
        cutoff = current - timedelta(days=max(1, int(source.get("recent_days") or 120)))
        tree = HTMLParser(document)
        output: list[Item] = []
        for node in tree.css(str(source.get("item_selector") or "li.no-point")):
            title_node = node.css_first(str(source.get("title_selector") or "a[href]"))
            date_node = node.css_first(str(source.get("date_selector") or "span.time"))
            if title_node is None or date_node is None:
                continue
            title = _clean(title_node.text(separator=" ", strip=True), 180)
            published = _date_value(date_node.text(separator=" ", strip=True))
            href = urljoin(
                str(source.get("list_url")), str(title_node.attributes.get("href") or "")
            )
            if not title or not published or not href.startswith(("http://", "https://")):
                continue
            if not cutoff <= published <= current + timedelta(days=1):
                continue
            hits = [
                str(value) for value in source.get("keywords", [])
                if str(value) and str(value).casefold() in title.casefold()
            ]
            output.append(Item(
                source="jobs", rank=len(output) + 1, title=title, title_zh=title,
                url=href, published_at=published.isoformat(),
                extra={
                    "subsource": str(source.get("subsource") or "chsi_talent"),
                    "source_label": str(source.get("name") or "教育部人才服务网"),
                    "company": _company_from_title(title), "city": "",
                    "keywords_hit": hits, "recruitment_type": "校园招聘",
                    "is_central_soe": False, "official_platform": True,
                },
            ))
        return output

    async def _fetch_source(self, source: Mapping[str, Any]) -> list[Item]:
        engine = str(source.get("engine") or "html").casefold()
        if engine == "api":
            keywords = [str(value).strip() for value in source.get("keywords", []) if str(value).strip()]
            if not keywords:
                keywords = [""]
            items: list[Item] = []
            params = dict(source.get("params") or {})
            for keyword in keywords:
                query = {**params, str(source.get("keyword_param") or "jobName"): keyword}
                response = await self.request(
                    str(source["list_url"]), params=query,
                    headers={
                        "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                        "Referer": str(source.get("referer") or source["list_url"]),
                    },
                )
                items.extend(self.parse_api(response.json(), source, keyword=keyword))
            return items
        if engine != "html":
            raise SourceUnavailable(f"Unsupported official jobs engine: {engine}", status="degraded")
        urls = source.get("list_urls") if isinstance(source.get("list_urls"), list) else [source["list_url"]]
        items = []
        for url in urls:
            response = await self.request(str(url), headers={"Accept": "text/html"})
            items.extend(self.parse_html(response.text, {**source, "list_url": str(url)}))
        return items

    async def fetch(self) -> list[Item]:
        sources = self.load_sources()
        self.failed_subsources.clear()
        results = await asyncio.gather(
            *(self._fetch_source(source) for source in sources), return_exceptions=True,
        )
        merged: dict[tuple[str, str], Item] = {}
        errors: list[str] = []
        for source, result in zip(sources, results, strict=True):
            name = str(source.get("name") or source.get("list_url"))
            if isinstance(result, BaseException):
                self.stats[name] = {"count": 0, "error": str(result)}
                self.failed_subsources.add(str(source.get("subsource") or ""))
                errors.append(f"{name}: {result}")
                continue
            self.stats[name] = {"count": len(result), "error": ""}
            for item in result:
                key = (item.title.casefold(), str(item.extra.get("company") or "").casefold())
                if key in merged:
                    merged[key].extra["keywords_hit"] = list(dict.fromkeys([
                        *merged[key].extra.get("keywords_hit", []),
                        *item.extra.get("keywords_hit", []),
                    ]))
                else:
                    merged[key] = item
        if not merged:
            raise SourceUnavailable(
                "Official job platforms failed or returned no rows: " + "; ".join(errors),
                status="degraded",
            )
        if errors:
            LOGGER.warning("Official job platforms partial failure: %s", "; ".join(errors))
        items, self.filter_stats, self.filtered_samples = filter_title_noise_items(merged.values())
        items.sort(key=lambda item: item.published_at or "", reverse=True)
        for rank, item in enumerate(items, 1):
            item.rank = rank
        return items
