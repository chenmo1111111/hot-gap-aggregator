from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml

from app.collectors.base import BaseCollector, SourceUnavailable, USER_AGENTS
from app.models import Item


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


def _plain(value: object, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def _published(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        stamp = int(value)
        if stamp > 10_000_000_000:
            stamp //= 1000
        try:
            return datetime.fromtimestamp(stamp, UTC).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    for candidate in (text, text.replace("/", "-")):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return parsed.isoformat()
        except ValueError:
            continue
    return text or None


class JobRadarCollector(BaseCollector):
    source = "jobs"
    timeout = 15.0
    tencent_endpoint = "https://careers.tencent.com/tencentcareer/api/post/Query"
    bytedance_endpoint = "https://jobs.bytedance.com/api/v1/search/job/posts"

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("JOB_RADAR_CONFIG", "config/job_radar.yaml"))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Job radar config missing: {self.config_path}", status="degraded")
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise SourceUnavailable("Job radar config must be a mapping", status="degraded")
        return config

    @staticmethod
    def parse_tencent(payload: dict[str, Any], keyword: str, limit: int) -> list[Item]:
        data = payload.get("Data") or {}
        posts = (data.get("Posts") or []) if isinstance(data, dict) else []
        items: list[Item] = []
        for row in posts[:limit]:
            if not isinstance(row, dict):
                continue
            title = _plain(row.get("RecruitPostName"), 160)
            url = str(row.get("PostURL") or "").strip()
            if not title or not url:
                continue
            items.append(Item(
                source="jobs", rank=0, title=title, title_zh=title,
                url=urljoin("https://careers.tencent.com/", url),
                summary_zh=_plain(row.get("Responsibility")),
                published_at=_published(row.get("LastUpdateTime")),
                extra={"company": "腾讯", "city": _plain(row.get("LocationName"), 80), "keywords_hit": [keyword]},
            ))
        return items

    @staticmethod
    def parse_bytedance(payload: dict[str, Any], keyword: str, limit: int) -> list[Item]:
        data = payload.get("data") or {}
        posts = (data.get("job_post_list") or []) if isinstance(data, dict) else []
        items: list[Item] = []
        for row in posts[:limit]:
            if not isinstance(row, dict):
                continue
            job_id, title = str(row.get("id") or "").strip(), _plain(row.get("title"), 160)
            if not job_id or not title:
                continue
            city_info = row.get("city_info") or {}
            city = city_info.get("name") if isinstance(city_info, dict) else city_info
            items.append(Item(
                source="jobs", rank=0, title=title, title_zh=title,
                url=f"https://jobs.bytedance.com/experienced/position/{job_id}/detail",
                summary_zh=_plain(row.get("description")), published_at=_published(row.get("publish_time")),
                extra={"company": "字节跳动", "city": _plain(city, 80), "keywords_hit": [keyword]},
            ))
        return items

    async def _fetch_tencent(self, keywords: list[str], limit: int) -> list[Item]:
        items: list[Item] = []
        errors: list[str] = []
        succeeded = 0
        for keyword in keywords:
            try:
                response = await self.request(self.tencent_endpoint, params={
                    "keyword": keyword, "pageIndex": 1, "pageSize": 20, "language": "zh-cn",
                }, headers={"Accept": "application/json"})
                payload = response.json()
                if int(payload.get("Code", 0)) != 200:
                    raise RuntimeError(f"Tencent API code={payload.get('Code')}")
                succeeded += 1
                items.extend(self.parse_tencent(payload, keyword, limit))
            except Exception as exc:
                errors.append(f"{keyword}: {exc}")
        if not succeeded:
            raise SourceUnavailable("Tencent jobs failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("Tencent jobs partial failure: %s", "; ".join(errors))
        return items

    async def _post_bytedance(self, payload: dict[str, Any]) -> httpx.Response:
        # These headers and body fields mirror the current official Careers bundle.
        # The site also computes a browser-only `_signature`; 405/risk responses are
        # therefore expected to degrade this subsource without affecting Tencent.
        headers = {
            "User-Agent": USER_AGENTS[0], "Accept": "application/json", "Content-Type": "application/json",
            "Portal-Channel": "pc", "Portal-Platform": "pc", "website-path": "experienced",
            "Accept-Language": "zh-CN,zh;q=0.9", "Referer": "https://jobs.bytedance.com/experienced/position",
        }
        body = {**payload, "portal_entrance": 1}
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.post(self.bytedance_endpoint, json=body)
                    response.raise_for_status()
                    return response
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * (2**attempt))
        raise SourceUnavailable(f"ByteDance jobs request failed: {last_error}", status="degraded")

    async def _fetch_bytedance(self, keywords: list[str], limit: int) -> list[Item]:
        items: list[Item] = []
        errors: list[str] = []
        succeeded = 0
        for keyword in keywords:
            try:
                response = await self._post_bytedance({
                    "keyword": keyword, "limit": 20, "offset": 0, "job_hot_flag": None,
                    "job_category_id_list": [], "tag_id_list": [], "location_code_list": [],
                    "subject_id_list": [], "recruitment_id_list": [], "portal_type": 2,
                    "job_function_id_list": [], "storefront_id_list": [], "job_post_id_list": [],
                })
                payload = response.json()
                if int(payload.get("code", -1)) != 0:
                    raise RuntimeError(f"ByteDance API code={payload.get('code')}")
                succeeded += 1
                items.extend(self.parse_bytedance(payload, keyword, limit))
            except Exception as exc:
                errors.append(f"{keyword}: {exc}")
        if not succeeded:
            raise SourceUnavailable("ByteDance jobs failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("ByteDance jobs partial failure: %s", "; ".join(errors))
        return items

    @staticmethod
    def _sort_timestamp(item: Item) -> float:
        try:
            value = datetime.fromisoformat(str(item.published_at or "").replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()
        except ValueError:
            return 0.0

    async def fetch(self) -> list[Item]:
        config = self.load_config()
        keywords = [str(value).strip() for value in config.get("keywords", []) if str(value).strip()]
        if not keywords:
            raise SourceUnavailable("Job radar has no keywords", status="degraded")
        limit = max(1, int(config.get("per_keyword_limit", 15)))
        results = await asyncio.gather(
            self._fetch_tencent(keywords, limit), self._fetch_bytedance(keywords, limit),
            return_exceptions=True,
        )
        merged: list[Item] = []
        errors: list[str] = []
        for provider, result in zip(("tencent", "bytedance"), results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{provider}: {result}")
            else:
                merged.extend(result)
        if len(errors) == 2:
            raise SourceUnavailable("All job providers failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("job radar partial failure: %s", "; ".join(errors))

        deduplicated: dict[tuple[str, str], Item] = {}
        for item in merged:
            key = (item.title.casefold(), str(item.extra.get("company") or "").casefold())
            if key in deduplicated:
                old_hits = list(map(str, deduplicated[key].extra.get("keywords_hit", [])))
                new_hits = list(map(str, item.extra.get("keywords_hit", [])))
                deduplicated[key].extra["keywords_hit"] = list(dict.fromkeys([*old_hits, *new_hits]))
            else:
                deduplicated[key] = item
        output = sorted(
            deduplicated.values(),
            key=lambda item: (-len(item.extra.get("keywords_hit", [])), -self._sort_timestamp(item)),
        )[:max(1, int(config.get("total_limit", 80)))]
        for rank, item in enumerate(output, 1):
            item.rank = rank
        return output
