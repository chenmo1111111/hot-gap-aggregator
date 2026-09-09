from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin

import yaml
from selectolax.parser import HTMLParser

from app.collectors.base import BaseCollector, SourceUnavailable
from app.collectors.gongkao_types import article_province, article_type, timeline_type
from app.models import Item

UTC = timezone.utc
CHINA_TZ = timezone(timedelta(hours=8))
LOGGER = logging.getLogger(__name__)
DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")
SHORT_DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[-/.月](\d{1,2})(?:日)?")
NOTICE_WORDS = ("公告", "招录", "招聘", "招考", "选调", "三支一扶", "文职", "警官")

# IDs are returned by /api/website/article/type and are stable Fenbi filter tags.
FOCUS_PROVINCES = {
    "北京": 159, "河北": 947, "黑龙江": 1307, "吉林": 1887,
    "辽宁": 2267, "内蒙古": 2416, "山东": 2894, "天津": 3406,
}
FOCUS_ARTICLE_EXAMS = {
    "公务员": 4001, "事业单位": 4002, "选调生": 4004,
    "三支一扶": 4007, "公安": 4006,
}


class GongkaoCollector(BaseCollector):
    source = "gongkao"
    article_endpoint = "https://hera-webapp.fenbi.com/api/website/article/hot/list/v3"
    chronological_endpoint = "https://hera-webapp.fenbi.com/api/website/article/filter/list/v2"
    condition_endpoint = "https://market-api.fenbi.com/toolkit/api/v1/pc/exam/queryByCondition"
    timeline_endpoint = "https://market-api.fenbi.com/toolkit/api/v1/timeline/getTimeLineDetails"
    common_params = {"app": "web", "av": 100, "hav": 100, "kav": 100, "client_context_id": ""}
    article_offsets = (0, 50, 100, 150)
    article_page_size = 50
    recent_days = 45
    recent_max_pages = 4
    concurrency = 5

    def __init__(
        self, watch_config: str | Path | None = None,
        fallback_config: str | Path | None = None,
    ) -> None:
        self.watch_config = Path(watch_config or os.getenv("GONGKAO_WATCH_CONFIG", "config/gongkao_watch.yaml"))
        self.fallback_config = Path(
            fallback_config
            or os.getenv("GONGKAO_FALLBACK_CONFIG", "config/gongkao_fallback_sources.yaml")
        )
        self._semaphore = asyncio.Semaphore(self.concurrency)

    @staticmethod
    def parse_articles(payload: dict) -> list[Item]:
        items: list[Item] = []
        for index, row in enumerate(payload.get("data", {}).get("articles", []), 1):
            article_id = row.get("id")
            title = str(row.get("title") or "").strip()
            if not article_id or not title:
                continue
            info = row.get("announcementArticleInfoRet") or {}
            raw_tags = [tag for tag in row.get("tagsList") or [] if isinstance(tag, dict)]
            tags = [str(tag.get("name")) for tag in raw_tags if tag.get("name")]
            location_tags = [
                str(tag.get("name")) for tag in raw_tags
                if tag.get("name") and int(tag.get("type") or 0) == 1
            ]
            education = next((
                name for name in tags
                if any(word in name for word in ("学历", "本科", "硕士", "博士", "大专", "专科"))
            ), None)
            items.append(Item(
                source="gongkao", rank=index, title=title, title_zh=title,
                url=f"https://hera-webapp.fenbi.com/api/website/article/detail?deviceType=3&id={article_id}&app=web&av=100&hav=100&kav=100&client_context_id=",
                hot_value=str(info.get("timeStatus")) if info.get("timeStatus") is not None else None,
                summary_zh=" ".join(str(row.get("digest") or row.get("preface") or "").split()).strip() or None,
                published_at=_timestamp(row.get("issueTime") or row.get("updateTime")),
                extra={
                    "id": article_id, "sub": "announcement", "tags": tags,
                    "province": article_province(raw_tags), "exam_type": article_type(raw_tags, title),
                    "startSignUpTime": info.get("enrollStartTime"), "endSignUpTime": info.get("enrollEndTime"),
                    "startWriteTime": info.get("writtenExamTime"),
                    "issueTime": row.get("issueTime"), "updateTime": row.get("updateTime"),
                    "recruit_count": info.get("recruitNumRet") or info.get("recruitNum"),
                    "position_count": info.get("positionNum"), "source_site": "fenbi",
                    "location": "·".join(dict.fromkeys(location_tags)) or None,
                    "education": education,
                },
            ))
        _annotate_record_kinds(items)
        return items

    @staticmethod
    def parse_timeline(payload: dict) -> list[Item]:
        rows = payload.get("datas") or payload.get("data", {}).get("datas") or []
        items: list[Item] = []
        for index, row in enumerate(rows, 1):
            event_id = row.get("id")
            title = str(row.get("topic") or "").strip()
            if not event_id or not title:
                continue
            start_signup = _date(row.get("startSignUpTime"))
            end_signup = _date(row.get("endSignUpTime"))
            write_time = _date(row.get("startWriteTime"))
            summary = f"（{start_signup or '待定'} 至 {end_signup or '待定'} 报名，{write_time or '待定'} 笔试）"
            type_code = row.get("examType") if row.get("examType") is not None else row.get("type")
            items.append(Item(
                source="gongkao", rank=index, title=title, title_zh=title,
                url=f"https://www.fenbi.com/page/kaoshidetail/{event_id}", summary_zh=summary,
                extra={
                    "id": event_id, "sub": "timeline", "startSignUpTime": row.get("startSignUpTime"),
                    "endSignUpTime": row.get("endSignUpTime"), "startWriteTime": row.get("startWriteTime"),
                    "province": row.get("province") or "全国", "type": row.get("type"),
                    "examType": row.get("examType"), "exam_type": timeline_type(type_code, title),
                },
            ))
        _annotate_record_kinds(items)
        return items

    async def _limited_get(self, url: str, **kwargs: Any):
        async with self._semaphore:
            response = await self.request(url, **kwargs)
            await asyncio.sleep(0.12)
            return response

    async def _limited_post(self, url: str, **kwargs: Any):
        async with self._semaphore:
            response = await self.post(url, **kwargs)
            await asyncio.sleep(0.12)
            return response

    async def _recent_article_combo(self, province_id: int, exam_id: int) -> list[Item]:
        cutoff = datetime.now(CHINA_TZ).date() - timedelta(days=self.recent_days)
        collected: list[Item] = []
        for page in range(self.recent_max_pages):
            response = await self._limited_get(
                self.chronological_endpoint,
                params={
                    **self.common_params, "province": province_id, "exam": exam_id,
                    "enrollStatus": 0, "recruit": 0,
                    "offset": page * self.article_page_size, "num": self.article_page_size,
                },
            )
            rows = response.json().get("data", {}).get("articles", [])
            page_items = self.parse_articles({"data": {"articles": rows}})
            recent = [item for item in page_items if (_item_date(item) or cutoff) >= cutoff]
            collected.extend(recent)
            if len(rows) < self.article_page_size or not recent:
                break
        return collected

    async def _condition_combo(self, district_id: int | None, exam_type: int) -> list[Item]:
        response = await self._limited_post(
            self.condition_endpoint,
            json={
                "districtId": district_id, "examType": exam_type, "year": None,
                "enrollStatus": None, "recruitNumCode": None,
                "start": 0, "len": 100, "needTotal": True,
            },
        )
        rows = response.json().get("data", {}).get("articles", [])
        cutoff = datetime.now(CHINA_TZ).date() - timedelta(days=self.recent_days)
        return [
            item for item in self.parse_articles({"data": {"articles": rows}})
            if (_item_date(item) or cutoff) >= cutoff
        ]

    def _fallback_sources(self) -> list[dict[str, str]]:
        if not self.fallback_config.exists():
            return []
        raw = yaml.safe_load(self.fallback_config.read_text(encoding="utf-8")) or {}
        rows = raw.get("sources", []) if isinstance(raw, Mapping) else []
        return [
            {str(key): str(value) for key, value in row.items()}
            for row in rows if isinstance(row, Mapping) and str(row.get("url") or "").strip()
        ]

    @staticmethod
    def parse_fallback_html(
        html: str, *, base_url: str, source_site: str, province: str = "全国",
        today: date | None = None,
    ) -> list[Item]:
        current = today or datetime.now(CHINA_TZ).date()
        cutoff = current - timedelta(days=45)
        items: list[Item] = []
        for anchor in HTMLParser(html).css("a[href]"):
            title = " ".join(str(anchor.attributes.get("title") or anchor.text() or "").split())
            if len(title) < 8 or not any(word in title for word in NOTICE_WORDS):
                continue
            href = urljoin(base_url, str(anchor.attributes.get("href") or "").strip())
            if not href.startswith(("http://", "https://")):
                continue
            context = title
            node = anchor
            for _ in range(3):
                node = node.parent
                if node is None:
                    break
                context += " " + " ".join(node.text().split())
            published = _date_from_text(context, current)
            if not published or published < cutoff or published > current + timedelta(days=1):
                continue
            inferred_province = next((name for name in FOCUS_PROVINCES if name in context), province)
            digest = hashlib.sha256(href.encode("utf-8")).hexdigest()[:24]
            items.append(Item(
                source="gongkao", rank=len(items) + 1, title=title, title_zh=title,
                url=href, published_at=published.isoformat(),
                extra={
                    "id": f"{source_site}:{digest}", "sub": "announcement",
                    "subsource": source_site,
                    "province": inferred_province, "exam_type": article_type([], title),
                    "source_site": source_site,
                },
            ))
        _annotate_record_kinds(items)
        return items

    async def _fetch_fallback_source(self, source: Mapping[str, str]) -> list[Item]:
        url = str(source.get("url") or "")
        response = await self._limited_get(url)
        return self.parse_fallback_html(
            _decode_html(response.content), base_url=url,
            source_site=str(source.get("name") or "fallback"),
            province=str(source.get("province") or "全国"),
        )

    async def fetch(self) -> list[Item]:
        article_requests = [
            self._limited_get(
                self.article_endpoint,
                params={**self.common_params, "offset": offset, "num": self.article_page_size},
            )
            for offset in self.article_offsets
        ]
        recent_requests = [
            self._recent_article_combo(province_id, exam_id)
            for province_id in (0, *FOCUS_PROVINCES.values())
            for exam_id in FOCUS_ARTICLE_EXAMS.values()
        ]
        # Fenbi's current endpoint exposes military/civilian and national joint
        # examinations which are absent from the legacy article-type list.
        condition_requests = [
            self._condition_combo(province_id or None, 2)
            for province_id in (0, *FOCUS_PROVINCES.values())
        ] + [self._condition_combo(None, 16)]
        fallback_requests = [self._fetch_fallback_source(source) for source in self._fallback_sources()]
        results = await asyncio.gather(
            *article_requests, *recent_requests, *condition_requests, *fallback_requests,
            return_exceptions=True,
        )
        items: list[Item] = []
        errors: list[str] = []
        hot_results = results[:len(article_requests)]
        for offset, result in zip(self.article_offsets, hot_results, strict=True):
            if isinstance(result, Exception):
                errors.append(f"article offset={offset}: {result}")
            else:
                items.extend(self.parse_articles(result.json()))
        for result in results[len(article_requests):]:
            if isinstance(result, Exception):
                errors.append(str(result))
                LOGGER.warning("Gongkao supplemental source failed: %s", result)
            else:
                items.extend(result)
        if not items:
            raise SourceUnavailable("; ".join(errors) or "Fenbi returned no items", status="degraded")
        items = _deduplicate_items(items)
        source_counts: dict[str, int] = {}
        for item in items:
            site = str(item.extra.get("source_site") or "unknown")
            source_counts[site] = source_counts.get(site, 0) + 1
        LOGGER.info("Gongkao collector complete: total=%d sources=%s", len(items), source_counts)
        self._annotate_watch_targets(items)
        for index, item in enumerate(items, 1):
            item.rank = index
        return items

    def _annotate_watch_targets(self, items: list[Item]) -> None:
        if not self.watch_config.exists():
            return
        raw = yaml.safe_load(self.watch_config.read_text(encoding="utf-8")) or {}
        universities = raw.get("target_universities", []) if isinstance(raw, dict) else []
        cities = raw.get("cities_focus", []) if isinstance(raw, dict) else []
        targets = [str(name).strip() for name in universities if str(name).strip()]
        city_targets = [str(name).strip() for name in cities if str(name).strip()]
        for item in items:
            haystack = f"{item.title} {item.summary_zh or ''}".casefold()
            item.extra["target_university_hit"] = [name for name in targets if name.casefold() in haystack]
            item.extra["city_focus_hit"] = [name for name in city_targets if name.casefold() in haystack]

    # Kept for callers/tests written against the older method name.
    def _annotate_target_universities(self, items: list[Item]) -> None:
        self._annotate_watch_targets(items)


def _timestamp(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        stamp = int(value)
        if stamp > 10_000_000_000:
            stamp //= 1000
        return datetime.fromtimestamp(stamp, CHINA_TZ).isoformat()
    return str(value)


def _date(value: object) -> str | None:
    timestamp = _timestamp(value)
    return timestamp[:10] if timestamp else None


def _item_date(item: Item) -> date | None:
    value = str(item.published_at or "")[:10]
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _date_from_text(value: str, today: date) -> date | None:
    match = DATE_PATTERN.search(value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = SHORT_DATE_PATTERN.search(value)
    if not match:
        return None
    try:
        candidate = date(today.year, int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None
    return candidate if candidate <= today + timedelta(days=1) else candidate.replace(year=today.year - 1)


def _decode_html(content: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _semantic_key(item: Item) -> tuple[str, str]:
    title = re.sub(r"[^\w]+", "", item.title.casefold())
    province = re.sub(r"(?:壮族|回族|维吾尔)?自治区$|省$|市$", "", str(
        item.extra.get("province") or "全国"
    ))
    return title, province


def _deduplicate_items(items: list[Item]) -> list[Item]:
    by_id: dict[tuple[str, str], Item] = {}
    by_semantic: dict[tuple[str, str], Item] = {}
    output: list[Item] = []
    for item in items:
        id_key = (str(item.extra.get("sub") or ""), str(item.extra.get("id") or ""))
        if id_key[1] and id_key in by_id:
            continue
        semantic = _semantic_key(item)
        existing = by_semantic.get(semantic)
        if existing is not None:
            if (
                str(existing.extra.get("source_site") or "") == "fenbi"
                and str(item.extra.get("source_site") or "") in {"huatu", "offcn"}
            ):
                old_url = existing.url
                existing.url = item.url
                existing.extra["fallback_url"] = item.url
                existing.extra["preferred_link_source"] = item.extra.get("source_site")
                existing.extra.setdefault("replaced_urls", []).append(old_url)
                if item.published_at:
                    existing.published_at = item.published_at
            continue
        output.append(item)
        if id_key[1]:
            by_id[id_key] = item
        if semantic[0]:
            by_semantic[semantic] = item
    return output


def _annotate_record_kinds(items: list[Item]) -> None:
    # Imported lazily to avoid the collectors package importing the pipeline
    # while its own modules are still being initialized.
    from app.pipeline.gongkao_classify import record_kind

    for item in items:
        item.extra["record_kind"] = record_kind(item.to_dict())
