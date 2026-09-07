from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.models import Item
from app.store.database import Database
from app.watchers.subsidy_watch import SubsidyWatcher, decode_response, parse_list_html


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
SELECTION_TOKENS = ("选调", "定向")
GXBYS_EMBEDDED_RE = re.compile(
    r'Base64\.decode\(unzip\("([A-Za-z0-9+/=]+)"\)\.substr\((\d+)\)\)\.substr\((\d+)\)'
)


def _company_from_title(title: str) -> str:
    text = re.sub(r"[｜|].*$", "", title).strip()
    text = re.sub(r"(?:校园)?招聘.*$", "", text).strip(" -—·：:")
    return text[:120] or "高校就业网"


def decode_gxbys_embedded_html(html_text: str) -> str | None:
    """Decode the compressed list fragment used by current GXBYS school sites."""
    fragments: list[str] = []
    for match in GXBYS_EMBEDDED_RE.finditer(html_text):
        try:
            compressed = base64.b64decode(match.group(1), validate=True)
            encoded = zlib.decompress(compressed).decode("ascii")
            payload = base64.b64decode(encoded[int(match.group(2)):], validate=True)
            fragment = payload.decode("utf-8")[int(match.group(3)):]
        except (ValueError, UnicodeDecodeError, zlib.error):
            continue
        if "<a" in fragment:
            fragments.append(fragment)
    return "\n".join(fragments) or None


def parse_campus_html(html_text: str, base_url: str) -> list[dict[str, str]]:
    """Parse current GXBYS pages and retain the generic-list fallback."""
    entries = parse_list_html(decode_gxbys_embedded_html(html_text) or html_text, base_url)
    unique: dict[str, dict[str, str]] = {}
    for entry in entries:
        url = entry["url"].rstrip("/")
        if url == base_url.rstrip("/"):
            continue
        unique.setdefault(url, entry)
    return list(unique.values())


class CampusJobsWatcher(SubsidyWatcher):
    """Domestic-side university recruitment-list watcher."""

    def __init__(
        self, database: Database, config_path: str | Path | None = None, **kwargs: Any,
    ) -> None:
        path = config_path or os.getenv("CAMPUS_JOBS_CONFIG", "config/campus_jobs_sources.yaml")
        super().__init__(database, path, **kwargs)
        self.latest_items: list[Item] = []
        self.latest_gongkao_items: list[Item] = []

    async def run(self) -> dict[str, list[dict[str, str]]]:
        config = self.load_config()
        keywords = [str(value).casefold() for value in config.get("title_keywords", []) if str(value).strip()]
        reports: list[dict[str, str]] = []
        jobs: list[Item] = []
        gongkao: list[Item] = []
        for page in config.get("list_pages", []):
            if not isinstance(page, dict) or not str(page.get("url") or "").startswith(("http://", "https://")):
                continue
            try:
                report, page_jobs, page_gongkao = await self.check_page(page, keywords)
                reports.append(report)
                jobs.extend(page_jobs)
                gongkao.extend(page_gongkao)
            except Exception as exc:
                school = str(page.get("school") or "")
                LOGGER.warning("campus jobs degraded (%s): %s", school, exc)
                reports.append({"school": school, "status": "degraded", "error": str(exc)})
        self.latest_items = self._deduplicate(jobs)
        self.latest_gongkao_items = self._deduplicate(gongkao)
        return {"list_pages": reports}

    async def check_page(
        self, page: dict[str, Any], keywords: list[str],
    ) -> tuple[dict[str, str], list[Item], list[Item]]:
        school, url = str(page.get("school") or ""), str(page["url"])
        response = await self._fetch_response(url)
        html_text = decode_response(response) if isinstance(response, httpx.Response) else str(response)
        entries = parse_campus_html(html_text, url)
        matched = [entry for entry in entries if any(keyword in entry["title"].casefold() for keyword in keywords)]
        event_pairs = [(f"campus:list:{school}:{entry['url']}", entry) for entry in matched]
        watch_key = f"campus:list:{school}:{url}"
        digest = hashlib.sha256(json.dumps(matched, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        previous = self.database.get_watcher_state(watch_key)
        unseen = self.database.unseen_push_events([key for key, _ in event_pairs]) if previous is not None else set()
        jobs = [self._job_item(page, entry, event_key in unseen) for event_key, entry in event_pairs]
        gongkao = [self._gongkao_item(page, entry, event_key in unseen) for event_key, entry in event_pairs if self._is_selection(entry["title"])]
        if previous is None:
            self.database.mark_push_events([key for key, _ in event_pairs])
            self.database.save_watcher_state(watch_key, digest, json.dumps(matched, ensure_ascii=False))
            return {"school": school, "status": "baseline", "item_count": str(len(matched))}, jobs, gongkao

        pushed = 0
        for event_key, entry in event_pairs:
            if event_key not in unseen:
                continue
            alert = self._campus_alert(page, entry)
            if await self._deliver(alert):
                self.database.mark_push_events([event_key])
                pushed += 1
        self.database.save_watcher_state(watch_key, digest, json.dumps(matched, ensure_ascii=False))
        return {
            "school": school, "status": "pushed" if pushed else "unchanged",
            "item_count": str(len(matched)), "pushed": str(pushed),
        }, jobs, gongkao

    @staticmethod
    def _is_selection(title: str) -> bool:
        return any(token in title for token in SELECTION_TOKENS)

    @staticmethod
    def _job_item(page: dict[str, Any], entry: dict[str, str], is_new: bool) -> Item:
        school = str(page.get("school") or "")
        city = str(page.get("city") or "哈尔滨")
        return Item(
            source="jobs", rank=0, title=entry["title"], title_zh=entry["title"], url=entry["url"],
            summary_zh=f"{school}就业信息网 · {city}", published_at=entry.get("date") or None,
            is_new=is_new,
            extra={
                "subsource": "campus", "school": school, "company": _company_from_title(entry["title"]),
                "city": city, "keywords_hit": [token for token in SELECTION_TOKENS if token in entry["title"]],
                "recruitment_type": "校园招聘", "is_central_soe": False,
            },
        )

    @staticmethod
    def _gongkao_item(page: dict[str, Any], entry: dict[str, str], is_new: bool) -> Item:
        school = str(page.get("school") or "")
        return Item(
            source="gongkao", rank=0, title=entry["title"], title_zh=entry["title"], url=entry["url"],
            summary_zh=f"{school}就业信息网选调公告", published_at=entry.get("date") or None,
            is_new=is_new,
            extra={
                "subsource": "campus", "school": school, "province": str(page.get("province") or "黑龙江"),
                "exam_type": "选调生", "target_university_hit": [school],
            },
        )

    @staticmethod
    def _deduplicate(items: list[Item]) -> list[Item]:
        unique = {item.url.rstrip("/").casefold(): item for item in items}
        output = sorted(unique.values(), key=lambda item: str(item.published_at or ""), reverse=True)
        for rank, item in enumerate(output, 1):
            item.rank = rank
        return output

    @staticmethod
    def _campus_alert(page: dict[str, Any], entry: dict[str, str]) -> dict[str, str]:
        created = datetime.now(UTC).isoformat(timespec="seconds")
        school = str(page.get("school") or "高校")
        selection = CampusJobsWatcher._is_selection(entry["title"])
        tag = "【选调预警·高校】" if selection else "【高校招聘】"
        return {
            "id": hashlib.sha256(f"campus:{school}:{entry['url']}".encode()).hexdigest()[:20],
            "tag": tag, "category_label": "选调预警" if selection else "高校招聘",
            "region": school, "type": "选调生公告" if selection else "校园招聘",
            "priority": "highest" if selection else "normal", "title": entry["title"],
            "url": entry["url"], "date": entry.get("date") or created[:10],
            "summary": f"来自{school}就业信息网", "created_at": created,
            "message": f"{tag}{entry['title']}｜{entry['url']}｜{entry.get('date') or created[:10]}",
        }
