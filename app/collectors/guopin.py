from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.collectors.base import BaseCollector, SourceUnavailable, USER_AGENTS
from app.models import Item


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


def _plain(value: object, limit: int = 240) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(value or "")).split())[:limit]


def _first(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, "", [], {}):
            return value
    return ""


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data: Any = payload.get("data")
    for _ in range(4):
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if not isinstance(data, dict):
            return []
        found = next((data.get(key) for key in ("list", "rows", "records", "job_list", "jobs", "items") if isinstance(data.get(key), list)), None)
        if found is not None:
            return [row for row in found if isinstance(row, dict)]
        data = data.get("data")
    return []


def _truthy(value: object) -> bool:
    return value is True or str(value).strip().casefold() in {"1", "true", "yes", "y"}


def parse_guopin(payload: dict[str, Any], keyword: str, provinces: list[str], limit: int) -> list[Item]:
    items: list[Item] = []
    for row in _rows(payload)[:limit]:
        company_info = row.get("company_info") if isinstance(row.get("company_info"), dict) else {}
        job_id = str(_first(row, "job_id", "id", "position_id")).strip()
        title = _plain(_first(row, "job_name", "title", "position_name"), 160)
        company = _plain(_first(row, "company_name", "company_show_name") or _first(company_info, "show_name", "company_name", "name"), 120)
        district = _first(row, "district_list", "work_city", "city_name", "location")
        if isinstance(district, list):
            city = "、".join(
                _plain(_first(value, "area_cn", "name", "city_name", "address") if isinstance(value, dict) else value, 80)
                for value in district
            )
        else:
            city = _plain(district, 100)
        if provinces and not any(province.casefold() in city.casefold() for province in provinces):
            continue
        if not job_id or not title:
            continue
        nature = _plain(
            _first(row, "recruitment_type_cn", "job_nature_cn", "recruit_type", "nature_cn")
            or _first(company_info, "nature_cn", "nature"),
            50,
        )
        company_nature = _plain(_first(company_info, "nature_cn", "nature"), 50)
        central = _truthy(_first(row, "is_central_soe", "is_central", "is_central_enterprise")) or "央企" in f"{company_nature}{company}"
        state_owned = central or any(token in f"{company_nature}{company}" for token in ("国企", "国有"))
        description = _plain(_first(row, "contents", "job_description_template", "job_description", "description", "duty"), 200)
        published = _plain(_first(row, "publish_time", "update_time", "create_time", "published_at"), 40) or None
        source_url = str(_first(row, "source_url", "job_url", "url")).strip()
        source = "campus" if nature in {"校园招聘", "校招"} else "social"
        url = source_url if source_url.startswith(("http://", "https://")) else f"https://www.iguopin.com/job/detail?id={job_id}&source={source}"
        items.append(Item(
            source="jobs", rank=0, title=title, title_zh=title, url=url,
            summary_zh=description, published_at=published,
            extra={
                "subsource": "guopin", "company": company or "国聘", "city": city,
                "education": _plain(_first(row, "education_cn", "education"), 60),
                "company_type": "央企" if central else ("国企" if state_owned else company_nature),
                "recruitment_type": nature, "keywords_hit": [keyword],
                "is_central_soe": central, "is_state_owned": state_owned,
            },
        ))
    return items


class GuopinCollector(BaseCollector):
    source = "jobs"
    timeout = 20.0

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("GUOPIN_CONFIG", "config/guopin.yaml"))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Guopin config missing: {self.config_path}", status="degraded")
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise SourceUnavailable("Guopin config must be a mapping", status="degraded")
        return config

    async def _post(self, url: str, body: dict[str, Any]) -> httpx.Response:
        headers = {
            "User-Agent": USER_AGENTS[0], "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json", "Device": "pc", "Version": "5.2.300",
            "Subsite": "iguopin", "Origin": "https://www.iguopin.com",
            "Referer": "https://www.iguopin.com/job",
        }
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.post(url, json=body)
                    response.raise_for_status()
                    return response
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * (2**attempt))
        raise SourceUnavailable(f"Guopin request failed: {last_error}", status="degraded")

    @staticmethod
    def _timestamp(item: Item) -> float:
        try:
            value = datetime.fromisoformat(str(item.published_at or "").replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()
        except ValueError:
            return 0.0

    async def fetch(self) -> list[Item]:
        config = self.load_config()
        endpoint = str(config.get("api_url") or "https://gp-api.iguopin.com/api/jobs/v1/recom-job")
        keywords = [str(value).strip() for value in config.get("keywords", []) if str(value).strip()]
        provinces = [str(value).strip() for value in config.get("provinces", []) if str(value).strip()]
        job_nature = [str(value).strip() for value in config.get("job_nature", []) if str(value).strip()]
        limit = max(1, int(config.get("per_query_limit", 25)))
        page_size = max(1, min(50, int(config.get("page_size", 20))))
        max_pages = max(1, int(config.get("max_pages", (limit + page_size - 1) // page_size)))
        lookback_days = max(1, int(config.get("lookback_days", 30)))
        if not keywords:
            raise SourceUnavailable("Guopin has no keywords", status="degraded")
        items: list[Item] = []
        errors: list[str] = []
        succeeded = 0
        for keyword in keywords:
            try:
                keyword_items: list[Item] = []
                for page in range(1, max_pages + 1):
                    body = {
                        "search": {"page": page, "page_size": page_size, "keyword": keyword},
                        "recom": {"update_time": True, "company_nature": True, "hot_job": True},
                    }
                    response = await self._post(endpoint, body)
                    payload = response.json()
                    raw_rows = _rows(payload)
                    if payload.get("data") is None:
                        raise RuntimeError(f"Guopin API returned no data (code={payload.get('code')})")
                    keyword_items.extend(parse_guopin(payload, keyword, provinces, page_size))
                    if len(raw_rows) < page_size or len(keyword_items) >= limit:
                        break
                succeeded += 1
                cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
                for item in keyword_items[:limit]:
                    timestamp = self._timestamp(item)
                    if timestamp and timestamp < cutoff.timestamp():
                        continue
                    if job_nature and item.extra.get("recruitment_type") and item.extra.get("recruitment_type") not in job_nature:
                        continue
                    items.append(item)
            except Exception as exc:
                errors.append(f"{keyword}: {exc}")
        if not succeeded:
            raise SourceUnavailable("Guopin jobs failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("Guopin partial failure: %s", "; ".join(errors))
        unique: dict[tuple[str, str], Item] = {}
        for item in items:
            key = (item.title.casefold(), str(item.extra.get("company") or "").casefold())
            if key in unique:
                unique[key].extra["keywords_hit"] = list(dict.fromkeys([*unique[key].extra.get("keywords_hit", []), *item.extra.get("keywords_hit", [])]))
            else:
                unique[key] = item
        output = sorted(unique.values(), key=lambda item: (-len(item.extra.get("keywords_hit", [])), -self._timestamp(item)))
        for rank, item in enumerate(output, 1):
            item.rank = rank
        return output
