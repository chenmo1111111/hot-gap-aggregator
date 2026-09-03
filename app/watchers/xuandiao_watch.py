from __future__ import annotations

import hashlib
import json
import logging
import os
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.models import Item
from app.store.database import Database
from app.watchers.subsidy_watch import SubsidyWatcher, decode_response, parse_list_html


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


def parse_hlj_api(payload: dict[str, Any]) -> list[dict[str, str]]:
    data = payload.get("data") or {}
    rows = (data.get("list") or []) if isinstance(data, dict) else []
    entries: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id, notice_type = str(row.get("id") or "").strip(), str(row.get("type") or "1").strip()
        title = " ".join(str(row.get("title") or "").replace("hh", "").split())
        if not title or not item_id:
            continue
        url = str(row.get("jumpUrl") or "").strip() or f"https://gongxuan.ljxfw.gov.cn/newsDetails.html?id={item_id}&type={notice_type}"
        entries.append({"title": title, "url": url, "date": str(row.get("publishDate") or row.get("publishTime") or "")[:10]})
    return entries


def parse_hebei_search(payload: dict[str, Any]) -> list[dict[str, str]]:
    data = payload.get("data") or {}
    rows = (data.get("rows") or []) if isinstance(data, dict) else []
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id, title = str(row.get("id") or "").strip(), " ".join(str(row.get("title") or "").split())
        url = str(row.get("jumpLink") or "").strip() or f"https://www.hebpta.com.cn/article?id={item_id}"
        if not title or not item_id or url in seen:
            continue
        seen.add(url)
        entries.append({"title": title, "url": url, "date": str(row.get("publishedDate") or "")[:10]})
    return entries


class XuandiaoWatcher(SubsidyWatcher):
    """Mainland-side official selection-graduate notice watcher."""

    def __init__(
        self, database: Database, config_path: str | Path | None = None, **kwargs: Any,
    ) -> None:
        path = config_path or os.getenv("XUANDIAO_SOURCES_CONFIG", "config/xuandiao_sources.yaml")
        super().__init__(database, path, **kwargs)
        self.latest_items: list[Item] = []

    async def run(self) -> dict[str, list[dict[str, str]]]:
        config = self.load_config()
        keywords = [str(value).casefold() for value in config.get("title_keywords", []) if str(value).strip()]
        universities = [str(value).strip() for value in config.get("my_universities", []) if str(value).strip()]
        reports: list[dict[str, str]] = []
        collected: list[Item] = []
        for index, page in enumerate(config.get("list_pages", [])):
            if not isinstance(page, dict) or not str(page.get("url") or "").startswith(("http://", "https://")):
                continue
            try:
                report, items = await self.check_list_page(page, keywords, universities, index)
                reports.append(report)
                collected.extend(items)
            except Exception as exc:
                LOGGER.warning("xuandiao list degraded (%s): %s", page.get("region"), exc)
                reports.append({"region": str(page.get("region") or ""), "status": "degraded", "error": str(exc)})
        unique: dict[str, Item] = {}
        for item in collected:
            unique.setdefault(item.url, item)
        self.latest_items = sorted(unique.values(), key=self._item_sort)
        for rank, item in enumerate(self.latest_items, 1):
            item.rank = rank
        return {"list_pages": reports}

    async def check_list_page(
        self, page: dict[str, Any], keywords: list[str], universities: list[str], priority: int,
    ) -> tuple[dict[str, str], list[Item]]:
        region, url = str(page.get("region") or ""), str(page["url"])
        page_format = str(page.get("format") or "html")
        if page_format == "hlj_api":
            response = await self._post_json(url, {"type": 1, "pageNum": 1, "pageSize": 50})
            payload = response.json() if isinstance(response, httpx.Response) else json.loads(str(response))
            if int(payload.get("code", 0)) != 200:
                raise RuntimeError(f"Heilongjiang API code={payload.get('code')}")
            entries = parse_hlj_api(payload)
        elif page_format == "hebei_search":
            response = await self._fetch_response(url)
            payload = response.json() if isinstance(response, httpx.Response) else json.loads(str(response))
            if str(payload.get("code")) != "200":
                raise RuntimeError(f"Hebei API code={payload.get('code')}")
            entries = parse_hebei_search(payload)
        else:
            response = await self._fetch_response(url, legacy_tls=bool(page.get("legacy_tls")))
            html_text = decode_response(response) if isinstance(response, httpx.Response) else str(response)
            entries = parse_list_html(html_text, url)
        matched = [entry for entry in entries if any(keyword in entry["title"].casefold() for keyword in keywords)]
        event_pairs = [(f"xuandiao:list:{region}:{entry['url']}", entry) for entry in matched]
        watch_key = f"xuandiao:list:{region}:{url}"
        digest = hashlib.sha256(json.dumps(matched, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        previous = self.database.get_watcher_state(watch_key)
        unseen = self.database.unseen_push_events([key for key, _ in event_pairs]) if previous is not None else set()

        items = [self._to_item(region, entry, universities, priority, event_key in unseen) for event_key, entry in event_pairs]
        if previous is None:
            self.database.mark_push_events([key for key, _ in event_pairs])
            self.database.save_watcher_state(watch_key, digest, json.dumps(matched, ensure_ascii=False))
            return {"region": region, "status": "baseline", "item_count": str(len(matched))}, items

        pending = [(key, entry) for key, entry in event_pairs if key in unseen]
        pending.sort(key=lambda pair: (0 if self._university_hits(pair[1]["title"], universities) else 1, priority, pair[1].get("date", "")), reverse=False)
        pushed = 0
        for event_key, entry in pending:
            school_hits = self._university_hits(entry["title"], universities)
            alert = self._alert(region, entry, school_hits)
            if await self._deliver(alert):
                self.database.mark_push_events([event_key])
                pushed += 1
        self.database.save_watcher_state(watch_key, digest, json.dumps(matched, ensure_ascii=False))
        return {
            "region": region, "status": "pushed" if pushed else "unchanged",
            "item_count": str(len(matched)), "pushed": str(pushed),
        }, items

    async def _post_json(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        last_error: Exception | None = None
        headers = {"User-Agent": "Mozilla/5.0 (compatible; hot-gap-xuandiao-watcher/1.0)", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.post(url, json=payload)
                    response.raise_for_status()
                    return response
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"xuandiao API request failed: {last_error}")

    @staticmethod
    def _university_hits(title: str, universities: list[str]) -> list[str]:
        folded = title.casefold()
        return [name for name in universities if name.casefold() in folded]

    @staticmethod
    def _to_item(region: str, entry: dict[str, str], universities: list[str], priority: int, is_new: bool) -> Item:
        school_hits = XuandiaoWatcher._university_hits(entry["title"], universities)
        return Item(
            source="gongkao", rank=0, title=entry["title"], title_zh=entry["title"], url=entry["url"],
            summary_zh=f"{region}官方选调生公告", published_at=entry.get("date") or None, is_new=is_new,
            extra={
                "subsource": "xuandiao", "province": region, "exam_type": "选调生",
                "target_university_hit": school_hits, "province_priority": priority,
            },
        )

    @staticmethod
    def _item_sort(item: Item) -> tuple[int, int, float]:
        try:
            published = datetime.fromisoformat(str(item.published_at or "")).replace(tzinfo=UTC).timestamp()
        except ValueError:
            published = 0.0
        return (
            0 if item.extra.get("target_university_hit") else 1,
            int(item.extra.get("province_priority", 999)),
            -published,
        )

    @staticmethod
    def _alert(region: str, entry: dict[str, str], school_hits: list[str]) -> dict[str, str]:
        created = datetime.now(UTC).isoformat(timespec="seconds")
        school = f"你的学校：{'、'.join(school_hits)}｜" if school_hits else ""
        tag = f"【选调预警·{region}】"
        message = f"{tag}{school}{entry['title']}｜{entry['url']}｜{entry.get('date') or created[:10]}"
        return {
            "id": hashlib.sha256(f"xuandiao:{region}:{entry['url']}".encode("utf-8")).hexdigest()[:20],
            "tag": tag, "category_label": "选调预警", "region": region, "type": "选调生公告",
            "priority": "highest" if school_hits else "normal", "title": entry["title"], "url": entry["url"],
            "date": entry.get("date") or created[:10], "summary": school.rstrip("｜"),
            "message": message, "created_at": created,
        }
