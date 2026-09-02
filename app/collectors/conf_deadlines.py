from __future__ import annotations

import asyncio
import html
import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item


AI_DEADLINE_URLS = (
    "https://raw.githubusercontent.com/huggingface/ai-deadlines/main/src/data/conferences.yml",
    "https://ccfddl.github.io/conference/allconf.yml",
    "https://raw.githubusercontent.com/paperswithcode/ai-deadlines/gh-pages/_data/conferences.yml",
)
WIKICFP_FEEDS = (
    "http://www.wikicfp.com/cfp/rss?cat=bioinformatics",
    "http://www.wikicfp.com/cfp/rss?cat=computational%20biology",
)
DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
)


def _plain(value: object) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def _timezone(value: object) -> timezone | ZoneInfo:
    label = str(value or "UTC").strip()
    if label.casefold() in {"aoe", "utc-12"}:
        return timezone(timedelta(hours=-12))
    match = re.fullmatch(r"(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", label, re.I)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0)))
    try:
        return ZoneInfo(label)
    except ZoneInfoNotFoundError:
        return UTC


def _deadline(value: object, timezone_name: object = "UTC") -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw or raw.casefold() in {"tba", "tbd", "none", "null", "n/a"}:
            return None
        raw = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
            for fmt in DATE_FORMATS:
                parsed = _strptime(raw, fmt)
                if parsed is not None:
                    break
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_timezone(timezone_name))
    return parsed.astimezone(UTC)


def _strptime(value: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(value, fmt)
    except ValueError:
        return None


def _watch_match(text: str, watch: list[str]) -> str | None:
    haystack = text.casefold()
    for conference in watch:
        token = conference.casefold().strip()
        if token and re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", haystack):
            return conference
    return None


class ConfDeadlinesCollector(BaseCollector):
    source = "conf_deadlines"
    timeout = 15.0

    def __init__(self, config_path: str | Path | None = None, *, now: datetime | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("CONFERENCES_CONFIG", "config/conferences.yaml"))
        self.now = (now or datetime.now(UTC)).astimezone(UTC)

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Conference config missing: {self.config_path}", status="degraded")
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise SourceUnavailable("Conference config must be a mapping", status="degraded")
        return raw

    def parse_ai_deadlines(self, yaml_text: str, watch: list[str]) -> list[Item]:
        rows = yaml.safe_load(yaml_text) or []
        if not isinstance(rows, list):
            return []
        items: list[Item] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_name = str(row.get("conf") or row.get("title") or row.get("name") or "").strip()
            conference = _watch_match(raw_name, watch)
            if not conference:
                continue
            editions = row.get("confs") if isinstance(row.get("confs"), list) else [row]
            for edition in editions:
                if not isinstance(edition, dict):
                    continue
                timelines = edition.get("timeline") if isinstance(edition.get("timeline"), list) else [edition]
                for timeline in timelines:
                    if not isinstance(timeline, dict):
                        continue
                    deadline = _deadline(timeline.get("deadline"), edition.get("timezone") or row.get("timezone"))
                    if not deadline:
                        continue
                    item = self._item(
                        conference, edition.get("year") or row.get("year"), deadline,
                        str(edition.get("link") or row.get("link") or "").strip(),
                        str(row.get("description") or row.get("full_name") or row.get("long") or conference).strip(),
                        str(edition.get("place") or row.get("place") or row.get("venue") or " · ".join(
                            str(part) for part in (row.get("city"), row.get("country")) if part
                        )).strip(),
                        "ai-deadlines",
                    )
                    if item:
                        items.append(item)
        return items

    def parse_wikicfp(self, xml_text: str, watch: list[str]) -> list[Item]:
        root = ET.fromstring(xml_text)
        items: list[Item] = []
        for node in root.findall(".//item"):
            values = {child.tag.rsplit("}", 1)[-1].casefold(): _plain("".join(child.itertext())) for child in node}
            title, description = values.get("title", ""), values.get("description", "")
            conference = _watch_match(f"{title} {description}", watch)
            if not conference:
                continue
            raw_deadline = values.get("deadline") or self._deadline_from_text(f"{title} {description}")
            deadline = _deadline(raw_deadline, values.get("timezone") or "UTC")
            if not deadline:
                continue
            year_match = re.search(r"\b20\d{2}\b", title)
            item = self._item(
                conference, year_match.group(0) if year_match else deadline.year, deadline,
                values.get("link", ""), title, values.get("location", ""), "wikicfp",
            )
            if item:
                items.append(item)
        return items

    @staticmethod
    def _deadline_from_text(text: str) -> str:
        match = re.search(
            r"(?:submission\s+)?deadline\s*[:\-]\s*"
            r"(20\d{2}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2})",
            text, re.I,
        )
        return match.group(1) if match else ""

    def _item(
        self, conference: str, year: object, deadline: datetime, link: str,
        full_name: str, location: str, subsource: str,
    ) -> Item | None:
        days_left = math.ceil((deadline - self.now).total_seconds() / 86400)
        if days_left < -7:
            return None
        year_text = str(year or deadline.year)
        summary = " · ".join(part for part in (full_name, location, "官网") if part)
        return Item(
            source=self.source, rank=0, title=f"{conference} {year_text} 截止",
            title_zh=f"{conference} {year_text} 截止", url=link or "https://www.wikicfp.com/",
            hot_value=f"距截止 {days_left} 天" if days_left >= 0 else f"已截止 {abs(days_left)} 天",
            summary_zh=summary, published_at=deadline.isoformat(),
            extra={
                "subsource": subsource, "conference": conference, "deadline": deadline.isoformat(),
                "days_left": days_left, "link": link, "full_name": full_name, "location": location,
            },
        )

    async def fetch(self) -> list[Item]:
        config = self.load_config()
        watch = [str(name).strip() for name in config.get("watch", []) if str(name).strip()]
        if not watch:
            raise SourceUnavailable("Conference watch list is empty", status="degraded")

        successes = 0
        errors: list[str] = []
        items: list[Item] = []
        ai_urls = tuple(config.get("ai_deadlines_urls") or AI_DEADLINE_URLS)
        for url in ai_urls:
            try:
                response = await self.request(str(url), headers={"Accept": "text/yaml,text/plain"})
                parsed = self.parse_ai_deadlines(response.text, watch)
                items.extend(parsed)
                successes += 1
                if parsed:
                    break
            except Exception as exc:
                errors.append(f"ai-deadlines {url}: {exc}")

        feeds = tuple(config.get("wikicfp_feeds") or WIKICFP_FEEDS)
        results = await asyncio.gather(
            *(self.request(str(url), headers={"Accept": "application/rss+xml,application/xml"}) for url in feeds),
            return_exceptions=True,
        )
        for url, result in zip(feeds, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"wikicfp {url}: {result}")
                continue
            try:
                items.extend(self.parse_wikicfp(result.text, watch))
                successes += 1
            except (ET.ParseError, ValueError) as exc:
                errors.append(f"wikicfp {url}: {exc}")

        if successes == 0:
            raise SourceUnavailable("All conference sources failed: " + "; ".join(errors), status="degraded")

        deduplicated: list[Item] = []
        seen: set[tuple[str, str]] = set()
        for item in sorted(items, key=lambda entry: (int(entry.extra["days_left"]), entry.title)):
            key = (str(item.extra["conference"]).casefold(), str(item.extra["deadline"]))
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)
        limit = max(1, int(config.get("limit", 30)))
        output = deduplicated[:limit]
        for rank, item in enumerate(output, 1):
            item.rank = rank
        return output
