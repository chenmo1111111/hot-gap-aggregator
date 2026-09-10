"""Public Xiaozhaoya aggregate collector.

The public page decrypts its own API response in the browser.  Reading the
page's Vue data avoids copying private cookies or depending on its encrypted
wire format.  Only public fields already rendered to visitors are collected.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from playwright.async_api import async_playwright
from selectolax.parser import HTMLParser

from app.pipeline.gongkao_classify import record_kind
from app.pipeline.gongkao_filter import is_gov_domain, title_noise_reason


PUBLIC_MARKERS = (
    "公务员", "事业单位", "事业编", "选调生", "三支一扶", "军队文职",
    "公安招警", "人民警察", "机关公开招聘", "政府招聘",
)
CAMPUS_MARKERS = ("校园招聘", "校招", "春招", "秋招", "应届生", "毕业生")
URL_PATTERN = re.compile(r"https?://[^\s，。；、<>\"']+", re.I)
DATE_PATTERN = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")


def _text(value: object) -> str:
    if isinstance(value, list):
        return "、".join(_text(item) for item in value if _text(item))
    return str(value or "").strip()


def _date(value: object) -> str:
    match = DATE_PATTERN.search(_text(value))
    if not match:
        return ""
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return ""


def _candidate_urls(value: object) -> list[str]:
    text = html.unescape(_text(value)).replace(r"\/", "/")
    if text.startswith(("http://", "https://")):
        values = [text]
    else:
        values = URL_PATTERN.findall(text)
    return [value.rstrip(")]},.!?;:") for value in values]


def resolve_public_url(value: object, *, base_url: str = "https://www.xiaozhaoya.com/home") -> str:
    """Return a usable public URL and unwrap Xiaozhaoya redirect pages."""
    candidates = _candidate_urls(value)
    if not candidates:
        text = _text(value)
        if text.startswith("/") and text != "/":
            candidates = [urljoin(base_url, text)]
    for candidate in candidates:
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        host = parsed.hostname.casefold()
        if not host.endswith("xiaozhaoya.com") or "redirect" not in parsed.path.casefold():
            return candidate
        try:
            response = httpx.get(candidate, timeout=10, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        final_host = (response.url.host or "").casefold()
        if final_host and not final_host.endswith("xiaozhaoya.com"):
            return str(response.url)
        tree = HTMLParser(response.text)
        for selector, attribute in (("a[href]", "href"), ("meta[http-equiv=refresh]", "content")):
            for node in tree.css(selector):
                target = _text(node.attributes.get(attribute))
                if attribute == "content" and "url=" in target.casefold():
                    target = re.split(r"url=", target, flags=re.I, maxsplit=1)[-1]
                target = urljoin(candidate, target.strip(" '\""))
                target_host = (urlsplit(target).hostname or "").casefold()
                if target_host and not target_host.endswith("xiaozhaoya.com"):
                    return target
    return ""


def _company_type(value: object) -> str:
    text = _text(value)
    if "央" in text:
        return "央企"
    if "国" in text:
        return "国企"
    if "外" in text:
        return "外企"
    if "银行" in text:
        return "银行"
    if "事业" in text:
        return "事业单位"
    return "民企" if "民" in text else "其他"


def _exam_type(title: str) -> str:
    for marker, label in (
        ("军队文职", "军队文职"), ("三支一扶", "三支一扶"), ("选调", "选调生"),
        ("公务员", "公务员"), ("公安", "公安"), ("警察", "公安"),
        ("事业单位", "事业单位"), ("事业编", "事业单位"),
    ):
        if marker in title:
            return label
    return "其他"


def split_records(records: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize public rows and route each one into exactly one paid dataset."""
    qiuzhao: list[dict[str, Any]] = []
    gongkao: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in records:
        identifier = _text(row.get("recruitmentId") or row.get("id"))
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        title = _text(row.get("announcementTitle") or row.get("jobTitle"))
        company = _text(row.get("fullName") or row.get("companyName"))
        position = _text(row.get("jobTitle") or title)
        if not title or not company or title_noise_reason({"title": title}):
            continue
        announcement_url = resolve_public_url(row.get("announcementLink"))
        apply_url = resolve_public_url(
            row.get("deliveryUrl") or row.get("applicationMethod") or row.get("announcementLink")
        )
        text = f"{title} {company} {_text(row.get('batchNameList'))}"
        mapped = {
            "title": title, "company": company,
            "extra": {"business_type": 4 if any(marker in text for marker in CAMPUS_MARKERS) else 0},
        }
        public_kind = any(marker in text for marker in PUBLIC_MARKERS) and record_kind(mapped) == "公考"
        published = _date(row.get("announcementDate") or row.get("updateDate"))
        if public_kind:
            target_url = announcement_url or apply_url
            if not target_url:
                continue
            exam_type = _exam_type(text)
            gongkao.append({
                "source": "gongkao", "rank": len(gongkao) + 1,
                "title": title, "title_zh": title, "url": target_url,
                "published_at": published or None,
                "summary_zh": _text(row.get("tip") or row.get("deliveryNotes")) or None,
                "extra": {
                    "id": f"xiaozhaoya:{identifier}", "sub": "announcement",
                    "source_site": "xiaozhaoya", "source_label": "校招鸭",
                    "upstream_source": "xiaozhaoya", "exam_type": exam_type,
                    "province": _text(row.get("provinceNameList")),
                    "city": _text(row.get("cityNameList")), "unit": company,
                    "endSignUpTime": _date(row.get("applicationDeadline")) or None,
                    "recruit_count": _text(row.get("recruitmentCount")),
                    "education": _text(row.get("educationLevelNameList")),
                    "has_announcement_structure": bool(
                        published or _date(row.get("applicationDeadline")) or row.get("recruitmentCount")
                    ),
                    "government_source": is_gov_domain(target_url),
                },
            })
            continue
        qiuzhao.append({
            "company_name": company,
            "company_type": _company_type(row.get("companyTypeName")),
            "industry": _text(row.get("industryName")),
            "position": position,
            "location": _text(row.get("cityNameList")),
            "education": _text(row.get("educationLevelNameList")),
            "cohort": _text(row.get("graduationYearList")),
            "deadline": _date(row.get("applicationDeadline")),
            "written_test": "笔试" in _text(row.get("hasWrittenTest")),
            "apply_url": apply_url,
            "announcement_url": announcement_url or apply_url,
            "notes": _text(row.get("deliveryNotes") or row.get("tip")),
            "updated_at": published,
            "source_record_id": f"xiaozhaoya:{identifier}",
            "source_label": "校招鸭",
            "upstream_source": "xiaozhaoya",
        })
    return qiuzhao, gongkao


class XiaozhaoyaCollector:
    def __init__(self, config_path: str | Path = "config/xiaozhaoya.yaml") -> None:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        self.page_url = _text(raw.get("page_url")) or "https://www.xiaozhaoya.com/home"
        self.page_size = max(12, int(raw.get("page_size") or 100))
        self.max_pages = max(1, int(raw.get("max_pages") or 5))
        self.max_age_days = max(1, int(raw.get("max_age_days") or 30))
        self.interval = max(1.0, float(raw.get("request_interval_seconds") or 1))
        self.minimum_items = max(1, int(raw.get("minimum_items") or 50))

    async def fetch_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cutoff = date.today() - timedelta(days=self.max_age_days)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await page.goto(self.page_url, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_function(
                    "() => [...document.querySelectorAll('*')].some(el => el.__vue__ && Array.isArray(el.__vue__.$data?.recruitmentList))",
                    timeout=60_000,
                )
                for page_number in range(1, self.max_pages + 1):
                    rows = await page.evaluate(
                        """async ({pageNumber, pageSize}) => {
                          const node = [...document.querySelectorAll('*')].find(
                            el => el.__vue__ && Array.isArray(el.__vue__.$data?.recruitmentList)
                          );
                          if (!node) throw new Error('Xiaozhaoya Vue recruitment list was not found');
                          const vm = node.__vue__;
                          vm.$data.currentPage = pageNumber;
                          vm.$data.pageSize = pageSize;
                          vm.$data.sortRule = 'updateDate';
                          await vm.fetchData();
                          return JSON.parse(JSON.stringify(vm.$data.recruitmentList || []));
                        }""",
                        {"pageNumber": page_number, "pageSize": self.page_size},
                    )
                    if not isinstance(rows, list) or not rows:
                        break
                    records.extend(row for row in rows if isinstance(row, dict))
                    dates = [_date(row.get("updateDate")) for row in rows if isinstance(row, dict)]
                    parsed = [date.fromisoformat(value) for value in dates if value]
                    if parsed and min(parsed) < cutoff:
                        break
                    await asyncio.sleep(self.interval)
            finally:
                await browser.close()
        unique_records = list({
            _text(row.get("recruitmentId") or row.get("id")): row
            for row in records
            if _text(row.get("recruitmentId") or row.get("id"))
        }.values())
        if len(unique_records) < self.minimum_items:
            raise RuntimeError(
                f"Xiaozhaoya capture too small: {len(unique_records)} < {self.minimum_items}; previous snapshot must be retained"
            )
        return unique_records

    async def collect(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return split_records(await self.fetch_records())


__all__ = ["XiaozhaoyaCollector", "resolve_public_url", "split_records"]
