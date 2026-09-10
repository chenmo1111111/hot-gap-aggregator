"""YAML-driven collector for authoritative government recruitment lists."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml
from selectolax.parser import HTMLParser, Node

from app.collectors.base import BaseCollector, SourceUnavailable, USER_AGENTS
from app.models import Item
from app.pipeline.gongkao_normalize import normalize_province


LOGGER = logging.getLogger(__name__)
CHINA_TZ = timezone(timedelta(hours=8))
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")
NOTICE_WORDS = ("公告", "招录", "招考", "招聘", "招募", "选调", "三支一扶", "文职", "军官", "警官", "招聘启事")


def _get_path(value: object, path: str) -> object:
    current = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _date_from_text(value: object) -> date | None:
    match = DATE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _source_date(
    value: object, source: Mapping[str, Any], *, current: date | None = None,
) -> date | None:
    text = str(value or "").strip()
    date_format = str(source.get("date_format") or "").strip()
    if date_format:
        try:
            parsed = datetime.strptime(text, date_format).date()
            if source.get("yearless_date"):
                reference = current or datetime.now(CHINA_TZ).date()
                parsed = parsed.replace(year=reference.year)
                if parsed > reference + timedelta(days=1):
                    parsed = parsed.replace(year=reference.year - 1)
            return parsed
        except ValueError:
            pass
    return _date_from_text(text)


def _decode(content: bytes, declared_encoding: object = "") -> str:
    head = content[:2048].decode("ascii", errors="ignore")
    meta = re.search(r"charset\s*=\s*['\"]?([\w-]+)", head, re.I)
    candidates = [str(declared_encoding or "").strip(), meta.group(1) if meta else "", "utf-8", "gb18030"]
    for encoding in dict.fromkeys(value for value in candidates if value):
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


def _node_text(node: Node | None) -> str:
    return " ".join(node.text(separator=" ", strip=True).split()) if node is not None else ""


class GovListCollector(BaseCollector):
    source = "gongkao_gov"
    timeout = 18.0
    retries = 2
    concurrency = 5
    request_interval = 0.2

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(
            config_path
            or os.getenv("GONGKAO_GOV_SOURCES_CONFIG", "config/gongkao_gov_sources.yaml")
        )
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self.stats: dict[str, dict[str, int | str]] = {}

    def load_sources(self) -> list[dict[str, Any]]:
        if not self.config_path.exists():
            return []
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        rows = raw.get("sources", []) if isinstance(raw, Mapping) else []
        output = [
            dict(row) for row in rows
            if isinstance(row, Mapping) and row.get("list_url") and row.get("enabled", True)
        ]
        for source in output:
            engine = str(source.get("engine") or "html").casefold()
            if engine in {"html", "playwright"}:
                required = ("item_selector", "title_selector", "link_selector", "date_selector")
                missing = [name for name in required if not str(source.get(name) or "").strip()]
                if missing:
                    raise ValueError(
                        f"{source.get('name') or source.get('list_url')}: empty selectors: {', '.join(missing)}"
                    )
        return output

    def load_seed_items(self) -> list[Item]:
        """Load a small official-URL safety net for intermittently blocked portals."""
        if str(os.getenv("GONGKAO_DISABLE_SEEDS") or "").strip().casefold() in {
            "1", "true", "yes", "on",
        }:
            return []
        if not self.config_path.exists():
            return []
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        rows = raw.get("seed_items", []) if isinstance(raw, Mapping) else []
        today = datetime.now(CHINA_TZ).date()
        max_hours = max(1, int(raw.get("seed_max_hours") or 72)) if isinstance(raw, Mapping) else 72
        # Config currently stores dates, so 72 hours is represented as three
        # calendar days.  Expired emergency rows cannot silently become a data source.
        cutoff = today - timedelta(hours=max_hours)
        output: list[Item] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            title = str(row.get("title") or "").strip()
            url = str(row.get("url") or "").strip()
            published = _date_from_text(row.get("published_at"))
            if (
                len(title) < 8 or not url.startswith(("http://", "https://"))
                or not published or not cutoff <= published <= today + timedelta(days=1)
            ):
                continue
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            output.append(Item(
                source="gongkao", rank=len(output) + 1, title=title, title_zh=title,
                url=url, published_at=published.isoformat(),
                extra={
                    "id": f"gov:{digest}", "sub": "announcement",
                    "subsource": "government", "source_site": "government",
                    "government_source": True,
                    "gov_source_name": str(row.get("source") or "政府官网兜底"),
                    "province": normalize_province(row.get("province")),
                    "exam_type": str(row.get("category") or "事业单位"),
                    "issueTime": published.isoformat(), "announcement_url": url,
                },
            ))
        return output

    @staticmethod
    def parse_html(
        document: str, source: Mapping[str, Any], *, today: date | None = None,
    ) -> list[Item]:
        current = today or datetime.now(CHINA_TZ).date()
        recent_days = max(1, int(source.get("recent_days") or 45))
        cutoff = current - timedelta(days=recent_days)
        # Several Hanweb government portals wrap generated list rows in XML CDATA.
        # Unwrapping it makes the saved response and the live response parse alike.
        cdata_rows = re.findall(r"<!\[CDATA\[(.*?)\]\]>", document, flags=re.S)
        if cdata_rows:
            document = "<html><body>" + "\n".join(cdata_rows) + "</body></html>"
        tree = HTMLParser(document)
        item_selector = str(source.get("item_selector") or "").strip()
        nodes = tree.css(item_selector) if item_selector else tree.css("a[href]")
        output: list[Item] = []
        seen: set[str] = set()
        for node in nodes:
            title_selector = str(source.get("title_selector") or "").strip()
            link_selector = str(source.get("link_selector") or "").strip()
            date_selector = str(source.get("date_selector") or "").strip()
            title_node = node.css_first(title_selector) if title_selector else node
            link_node = node.css_first(link_selector) if link_selector else (
                node if node.attributes.get("href") else node.css_first("a[href]")
            )
            date_node = node.css_first(date_selector) if date_selector else None
            title = " ".join(str(
                (title_node.attributes.get("title") if title_node else "") or _node_text(title_node)
            ).split())
            if len(title) < 8 or not any(word in title for word in NOTICE_WORDS):
                continue
            title_include = str(source.get("title_include") or "").strip()
            if title_include and re.search(title_include, title, re.I) is None:
                continue
            href = urljoin(
                str(source.get("list_url")),
                str(link_node.attributes.get("href") if link_node else "").strip(),
            )
            if not href.startswith(("http://", "https://")) or href in seen:
                continue
            context = _node_text(node)
            if not date_selector:
                parent = node
                for _ in range(2):
                    parent = parent.parent
                    if parent is None:
                        break
                    context += " " + _node_text(parent)
            published = _source_date(
                _node_text(date_node) if date_node else context, source, current=current,
            )
            if not published or published < cutoff or published > current + timedelta(days=1):
                continue
            seen.add(href)
            digest = hashlib.sha256(href.encode("utf-8")).hexdigest()[:24]
            output.append(Item(
                source="gongkao", rank=len(output) + 1, title=title, title_zh=title,
                url=href, published_at=published.isoformat(),
                extra={
                    "id": f"gov:{digest}", "sub": "announcement",
                    "subsource": "government", "source_site": "government",
                    "government_source": True,
                    "gov_source_name": str(source.get("name") or "政府公开招聘"),
                    "province": normalize_province(source.get("province")),
                    "exam_type": str(source.get("category") or "事业单位"),
                    "issueTime": published.isoformat(),
                    "announcement_url": href,
                },
            ))
        return output

    @staticmethod
    def parse_api(payload: object, source: Mapping[str, Any], *, today: date | None = None) -> list[Item]:
        current = today or datetime.now(CHINA_TZ).date()
        cutoff = current - timedelta(days=max(1, int(source.get("recent_days") or 45)))
        rows = _get_path(payload, str(source.get("items_path") or "data.items"))
        if not isinstance(rows, list):
            return []
        fields = source.get("fields") if isinstance(source.get("fields"), Mapping) else {}
        output: list[Item] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            title = str(_get_path(row, str(fields.get("title") or "title")) or "").strip()
            title_include = str(source.get("title_include") or "").strip()
            if title_include and re.search(title_include, title, re.I) is None:
                continue
            link_value = str(_get_path(row, str(fields.get("link") or "url")) or "")
            link_template = str(source.get("link_template") or "").strip()
            link = (
                link_template.format(value=link_value)
                if link_template else urljoin(str(source.get("list_url")), link_value)
            )
            published = _source_date(
                _get_path(row, str(fields.get("date") or "date")), source, current=current,
            )
            if (
                len(title) < 8 or not link.startswith(("http://", "https://"))
                or not published or not cutoff <= published <= current + timedelta(days=1)
            ):
                continue
            digest = hashlib.sha256(link.encode("utf-8")).hexdigest()[:24]
            output.append(Item(
                source="gongkao", rank=len(output) + 1, title=title, title_zh=title,
                url=link, published_at=published.isoformat(),
                extra={
                    "id": f"gov:{digest}", "sub": "announcement",
                    "subsource": "government", "source_site": "government",
                    "government_source": True,
                    "gov_source_name": str(source.get("name") or "政府公开招聘"),
                    "province": normalize_province(source.get("province")),
                    "exam_type": str(source.get("category") or "事业单位"),
                    "issueTime": published.isoformat(), "announcement_url": link,
                },
            ))
        return output

    @staticmethod
    def _source_urls(source: Mapping[str, Any]) -> list[str]:
        explicit = source.get("list_urls")
        if isinstance(explicit, list):
            urls = [str(value).strip() for value in explicit if str(value).strip()]
            if urls:
                return urls
        template = str(source.get("page_url_template") or "").strip()
        if template:
            start = int(source.get("page_start") or 1)
            count = max(1, int(source.get("page_count") or 1))
            return [template.format(page=page) for page in range(start, start + count)]
        return [str(source["list_url"])]

    async def _html(self, source: Mapping[str, Any], url: str) -> str:
        engine = str(source.get("engine") or "html").casefold()
        if engine != "playwright":
            response = await self.request(url, headers={"Accept": "text/html,application/xhtml+xml"})
            await asyncio.sleep(self.request_interval)
            return _decode(response.content, source.get("encoding") or response.encoding)
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=USER_AGENTS[0])
            try:
                last_error: Exception | None = None
                for attempt in range(self.retries + 1):
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=30_000)
                        return await page.content()
                    except Exception as exc:
                        last_error = exc
                        if attempt < self.retries:
                            await asyncio.sleep(0.5 * (2**attempt))
                raise SourceUnavailable(f"playwright failed after retries: {last_error}", status="degraded")
            finally:
                await browser.close()

    async def _fetch_one(self, source: Mapping[str, Any]) -> tuple[str, list[Item], str | None]:
        name = str(source.get("name") or source.get("list_url"))
        try:
            async with self._semaphore:
                engine = str(source.get("engine") or "html").casefold()
                if engine == "api":
                    method = str(source.get("method") or "GET").upper()
                    kwargs: dict[str, Any] = {}
                    if isinstance(source.get("params"), Mapping):
                        kwargs["params"] = dict(source["params"])
                    if isinstance(source.get("json"), Mapping):
                        kwargs["json"] = dict(source["json"])
                    if source.get("verify_tls") is False:
                        kwargs["verify"] = False
                    response = await self._request(method, str(source["list_url"]), **kwargs)
                    items = self.parse_api(response.json(), source)
                    await asyncio.sleep(self.request_interval)
                else:
                    items = []
                    seen: set[str] = set()
                    page_errors: list[str] = []
                    for url in self._source_urls(source):
                        page_source = dict(source)
                        page_source["list_url"] = url
                        try:
                            page_items = self.parse_html(await self._html(page_source, url), page_source)
                        except Exception as exc:
                            page_errors.append(f"{url}: {exc}")
                            continue
                        for item in page_items:
                            if item.url not in seen:
                                seen.add(item.url)
                                items.append(item)
                    if not items and page_errors:
                        raise SourceUnavailable("; ".join(page_errors), status="degraded")
            return name, items, None
        except Exception as exc:  # one government portal must never block the others
            return name, [], str(exc)

    async def fetch(self) -> list[Item]:
        sources = self.load_sources()
        if not sources:
            raise SourceUnavailable("government source config is empty", status="skipped")
        results = await asyncio.gather(*(self._fetch_one(source) for source in sources))
        output: list[Item] = self.load_seed_items()
        today = datetime.now(CHINA_TZ).date()
        if output:
            seed_dates = [
                parsed for item in output
                if (parsed := _date_from_text(item.published_at)) is not None
            ]
            self.stats["官方链接兜底"] = {
                "count": len(output),
                "last_7_days": sum(value >= today - timedelta(days=7) for value in seed_dates),
                "last_30_days": sum(value >= today - timedelta(days=30) for value in seed_dates),
                "error": "",
            }
        for name, items, error in results:
            dates = [
                parsed for item in items
                if (parsed := _date_from_text(item.published_at)) is not None
            ]
            self.stats[name] = {
                "count": len(items),
                "last_7_days": sum(value >= today - timedelta(days=7) for value in dates),
                "last_30_days": sum(value >= today - timedelta(days=30) for value in dates),
                "error": error or ("reachable_but_no_recent_dated_rows" if not items else ""),
            }
            if error:
                LOGGER.warning("Government Gongkao source failed: %s: %s", name, error)
            else:
                LOGGER.info("Government Gongkao source complete: %s count=%d", name, len(items))
            output.extend(items)
        if not output and all(error for _, _, error in results):
            raise SourceUnavailable("all government Gongkao sources failed", status="degraded")
        return output
