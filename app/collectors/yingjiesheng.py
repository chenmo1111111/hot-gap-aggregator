from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import yaml
from playwright.async_api import async_playwright
from selectolax.parser import HTMLParser

from app.collectors.base import BaseCollector, SourceUnavailable, USER_AGENTS
from app.models import Item
from app.pipeline.gongkao_filter import filter_title_noise_items


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")
WAF_MARKERS = ("aliyun_waf_aa", "aliyun_waf", "acw_sc__v2", "waf challenge")
FALLBACK_URLS = {
    "haitou": "https://sx.haitou.cc/article/list?key={keyword}&select_classify=1&select_type=3&type=1",
    "wutongguo": "https://www.wutongguo.com/search?keyword={keyword}",
}


def _clean(value: object, limit: int = 240) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(value or "")).split())[:limit]


def decode_yingjiesheng_html(content: bytes) -> str:
    """Honor the legacy GBK declaration used by the working XJH endpoint."""
    head = content[:4096].lower()
    encoding = "gb18030" if b"charset=\"gbk\"" in head or b"charset=gbk" in head else "utf-8"
    return content.decode(encoding, errors="replace")


def _iso_date(value: str) -> str | None:
    match = DATE_RE.search(value)
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def is_waf_challenge(html_text: str) -> bool:
    """Recognize the official site's Alibaba WAF page and avoid a long selector wait."""
    lowered = html_text.casefold()
    return any(marker in lowered for marker in WAF_MARKERS) or "请按住滑块" in html_text


def parse_fallback_html(
    html_text: str, base_url: str, source: str, keyword: str,
    cities: list[str], limit: int,
) -> list[Item]:
    """Parse a conservative subset of Haitou/Wutongguo recruitment cards."""
    tree = HTMLParser(html_text)
    items: list[Item] = []
    seen: set[str] = set()
    path_tokens = ("/article/", "/job/", "/position/")
    for anchor in tree.css("a[href]"):
        href = urljoin(base_url, str(anchor.attributes.get("href") or ""))
        if not any(token in urlparse(href).path.casefold() for token in path_tokens):
            continue
        title_node = anchor.css_first("h1,h2,h3,h4,strong,b")
        title = _clean(title_node.text(separator=" ", strip=True) if title_node else anchor.text(separator=" ", strip=True), 160)
        if len(title) < 4 or href in seen:
            continue
        container = anchor.parent or anchor
        block = _clean(container.text(separator=" ", strip=True), 1200)
        city = next((name for name in cities if name.casefold() in block.casefold()), "")
        if cities and not city:
            continue
        company_match = re.search(r"([\u4e00-\u9fffA-Za-z0-9（）()·]{2,80}(?:公司|集团|研究院|研究所|中心))", block)
        company = _clean(company_match.group(1) if company_match else source, 120)
        seen.add(href)
        items.append(Item(
            source="jobs", rank=0, title=title, title_zh=title, url=href,
            summary_zh=_clean(block.replace(title, "").replace(company, ""), 200),
            published_at=_iso_date(block),
            extra={
                "subsource": source, "company": company, "city": city,
                "keywords_hit": [keyword], "recruitment_type": "校园招聘",
                "is_central_soe": False,
            },
        ))
        if len(items) >= limit:
            break
    return items


def _search_property(href: str) -> dict[str, Any]:
    raw = parse_qs(urlparse(href).query).get("property", [""])[0]
    if not raw:
        return {}
    try:
        value = json.loads(unquote(raw))
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def parse_search_html(
    html_text: str, keyword: str, cities: list[str], types: list[str], limit: int,
) -> list[Item]:
    tree = HTMLParser(html_text)
    items: list[Item] = []
    seen: set[str] = set()
    for anchor in tree.css('a[href*="/jobdetail/"]'):
        href = urljoin("https://q.yingjiesheng.com/", str(anchor.attributes.get("href") or ""))
        if not href or href in seen:
            continue
        text = _clean(anchor.text(separator=" ", strip=True), 1200)
        meta = _search_property(href)
        title = _clean(meta.get("jobTitle") or meta.get("jobName"), 160)
        company = _clean(meta.get("companyName") or meta.get("company"), 120)
        if not title:
            candidates = [
                _clean(node.text(separator=" ", strip=True), 160)
                for node in anchor.css("h1,h2,h3,h4,strong,b")
            ]
            title = next((value for value in candidates if value), "")
        if not title:
            continue
        city = next((name for name in cities if name.casefold() in text.casefold()), "")
        recruitment_type = "校园招聘" if any(token in text for token in ("校园招聘", "校招", "应届", "在校生")) else ""
        explicitly_social = any(token in text for token in ("社会招聘", "社招"))
        if types and recruitment_type and recruitment_type not in types:
            continue
        if types == ["校园招聘"] and explicitly_social and not recruitment_type:
            continue
        if cities and not city:
            continue
        published = _iso_date(text)
        seen.add(href)
        items.append(Item(
            source="jobs", rank=0, title=title, title_zh=title, url=href,
            summary_zh=_clean(text.replace(title, "").replace(company, ""), 200),
            published_at=published,
            extra={
                "subsource": "yingjiesheng", "company": company or "应届生求职网",
                "city": city, "keywords_hit": [keyword],
                "recruitment_type": recruitment_type or "校园招聘",
                "is_central_soe": False,
            },
        ))
        if len(items) >= limit:
            break
    return items


def parse_xjh_html(html_text: str, cities: list[str], limit: int) -> list[Item]:
    tree = HTMLParser(html_text)
    items: list[Item] = []
    seen: set[str] = set()
    rows = tree.css("div.listul tr")
    if not rows:
        rows = [node.css_first("tr") or node for node in tree.css("table.li, table[class*=li]")]
    for row in rows:
        cells = row.css("td")
        if len(cells) < 6:
            continue
        city = _clean(cells[0].text(separator=" ", strip=True), 40)
        if cities and not any(name.casefold() in city.casefold() for name in cities):
            continue
        date = _iso_date(_clean(cells[1].text(separator=" ", strip=True), 80))
        company_node = cells[3].css_first("a[href]")
        school_node = cells[4].css_first("a[href]")
        link_node = cells[-1].css_first("a[href]")
        company = _clean(company_node.text(strip=True) if company_node else cells[3].text(strip=True), 120)
        school = _clean(school_node.text(strip=True) if school_node else cells[4].text(strip=True), 120)
        location = _clean(cells[5].text(separator=" ", strip=True), 120)
        href = urljoin("https://my.yingjiesheng.com/", str((link_node or company_node).attributes.get("href") if (link_node or company_node) else ""))
        if not company or not href or href in seen:
            continue
        seen.add(href)
        items.append(Item(
            source="jobs", rank=0, title=f"{company} 宣讲会", title_zh=f"{company} 宣讲会", url=href,
            summary_zh=" · ".join(value for value in (school, location) if value), published_at=date,
            extra={
                "subsource": "xjh", "company": company, "city": city, "school": school,
                "location": location, "keywords_hit": [], "recruitment_type": "校园招聘",
                "is_central_soe": False,
            },
        ))
        if len(items) >= limit:
            break
    return items


class YingjieshengCollector(BaseCollector):
    source = "jobs"
    timeout = 20.0
    # The legacy official endpoint currently redirects to q.yingjiesheng.com;
    # keep both current routes because the SPA has changed paths more than once.
    search_urls = (
        "https://s.yingjiesheng.com/search.php?word={keyword}",
        "https://q.yingjiesheng.com/pc/search?keyword={keyword}",
    )
    xjh_url = "https://my.yingjiesheng.com/xuanjianghui.html"

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("YINGJIESHENG_CONFIG", "config/yingjiesheng.yaml"))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Yingjiesheng config missing: {self.config_path}", status="degraded")
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise SourceUnavailable("Yingjiesheng config must be a mapping", status="degraded")
        return config

    async def _fetch_search(
        self, keywords: list[str], cities: list[str], types: list[str], limit: int,
        fallback_source: str = "", fallback_url: str = "",
    ) -> list[Item]:
        items: list[Item] = []
        errors: list[str] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=USER_AGENTS[0], viewport={"width": 1440, "height": 900})
                page = await context.new_page()
                for keyword in keywords:
                    keyword_errors: list[str] = []
                    for template in self.search_urls:
                        try:
                            from urllib.parse import quote
                            await page.goto(template.format(keyword=quote(keyword)), wait_until="domcontentloaded", timeout=30_000)
                            initial_html = await page.content()
                            if is_waf_challenge(initial_html):
                                keyword_errors.append(f"{template}: blocked by site WAF")
                                continue
                            await page.wait_for_selector('a[href*="/jobdetail/"]', timeout=15_000)
                            parsed = parse_search_html(await page.content(), keyword, cities, types, limit)
                            if parsed:
                                items.extend(parsed)
                                break
                            keyword_errors.append(f"{template}: no matching cards")
                        except Exception as exc:
                            keyword_errors.append(f"{template}: {exc}")
                    else:
                        errors.append(f"{keyword}: {'; '.join(keyword_errors)}")
                if not items and fallback_source:
                    template = fallback_url or FALLBACK_URLS.get(fallback_source, "")
                    if template:
                        from urllib.parse import quote
                        for keyword in keywords:
                            try:
                                url = template.format(keyword=quote(keyword))
                                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                                await page.wait_for_timeout(1500)
                                html_text = await page.content()
                                if is_waf_challenge(html_text):
                                    raise RuntimeError("blocked by fallback site WAF")
                                items.extend(parse_fallback_html(
                                    html_text, url, fallback_source, keyword, cities, limit,
                                ))
                            except Exception as exc:
                                errors.append(f"fallback {fallback_source}/{keyword}: {exc}")
                await context.close()
            finally:
                await browser.close()
        if not items and errors:
            raise SourceUnavailable("Yingjiesheng search failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("Yingjiesheng search partial failure: %s", "; ".join(errors))
        return items

    async def _fetch_xjh(self, cities: list[str], limit: int) -> list[Item]:
        response = await self.request(self.xjh_url, headers={"Accept": "text/html"})
        return parse_xjh_html(decode_yingjiesheng_html(response.content), cities, limit)

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
        types = [str(value).strip() for value in config.get("types", []) if str(value).strip()]
        fallback_source = str(config.get("fallback_source") or "").strip().casefold()
        fallback_url = str(config.get("fallback_url") or "").strip()
        limit = max(1, int(config.get("per_query_limit", 20)))
        if not keywords:
            raise SourceUnavailable("Yingjiesheng has no keywords", status="degraded")
        import asyncio
        # 宣讲会/行程不是可投递岗位，不再请求或生成这类条目。
        results = await asyncio.gather(
            self._fetch_search(keywords, cities, types, limit, fallback_source, fallback_url),
            return_exceptions=True,
        )
        merged: list[Item] = []
        errors: list[str] = []
        for name, result in zip(("search",), results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{name}: {result}")
            else:
                merged.extend(result)
        if len(errors) == 1:
            raise SourceUnavailable("All Yingjiesheng providers failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("Yingjiesheng partial failure: %s", "; ".join(errors))
        cutoff = datetime.now(UTC).date() - timedelta(days=max(1, int(config.get("lookback_days", 5))))
        unique: dict[tuple[str, str], Item] = {}
        for item in merged:
            if item.published_at:
                try:
                    if datetime.fromisoformat(item.published_at[:10]).date() < cutoff:
                        continue
                except ValueError:
                    pass
            key = (item.title.casefold(), str(item.extra.get("company") or "").casefold())
            if key in unique:
                old = unique[key]
                old.extra["keywords_hit"] = list(dict.fromkeys([*old.extra.get("keywords_hit", []), *item.extra.get("keywords_hit", [])]))
            else:
                unique[key] = item
        output = sorted(unique.values(), key=lambda item: (-len(item.extra.get("keywords_hit", [])), -self._timestamp(item)))
        output, self.filter_stats, self.filtered_samples = filter_title_noise_items(output)
        for rank, item in enumerate(output, 1):
            item.rank = rank
        return output
