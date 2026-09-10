"""Resolve Fenbi discoveries to verified official announcement pages."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from selectolax.parser import HTMLParser

from app.models import Item
from app.pipeline.gongkao_normalize import clean_official_title, normalize_notice_title


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
Request = Callable[..., Awaitable[httpx.Response]]


def _title_similarity(left: object, right: object) -> float:
    a = normalize_notice_title(left)
    b = normalize_notice_title(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    return SequenceMatcher(None, a, b).ratio()


class OfficialDomainPolicy:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.suffixes = tuple(str(value).casefold() for value in config.get("suffixes", []) if value)
        self.known_hosts = {str(value).casefold().rstrip(".") for value in config.get("known_hosts", []) if value}

    def allows(self, url: object) -> bool:
        try:
            host = (urlsplit(str(url or "")).hostname or "").casefold().rstrip(".")
        except ValueError:
            return False
        return bool(host) and (host in self.known_hosts or any(host.endswith(suffix) for suffix in self.suffixes))


class ResolverCache:
    def __init__(self, path: str | Path, ttl_hours: int = 24) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=max(1, ttl_hours))
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS gongkao_official_link_cache (
                query_key TEXT PRIMARY KEY,
                source_title TEXT NOT NULL,
                result_url TEXT NOT NULL DEFAULT '',
                result_title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL
            )
        """)
        self.connection.commit()

    def get(self, title: str) -> dict[str, str] | None:
        key = hashlib.sha256(normalize_notice_title(title).encode("utf-8")).hexdigest()
        row = self.connection.execute(
            "SELECT * FROM gongkao_official_link_cache WHERE query_key = ?", (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            checked = datetime.fromisoformat(str(row["checked_at"]))
        except ValueError:
            return None
        if datetime.now(UTC) - checked.astimezone(UTC) >= self.ttl:
            return None
        return {name: str(row[name]) for name in row.keys()}

    def put(self, title: str, *, url: str = "", result_title: str = "", status: str) -> None:
        key = hashlib.sha256(normalize_notice_title(title).encode("utf-8")).hexdigest()
        self.connection.execute(
            """INSERT INTO gongkao_official_link_cache
               (query_key, source_title, result_url, result_title, status, checked_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(query_key) DO UPDATE SET
                 source_title=excluded.source_title, result_url=excluded.result_url,
                 result_title=excluded.result_title, status=excluded.status,
                 checked_at=excluded.checked_at""",
            (key, title, url, result_title, status, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def parse_bing_results(document: str) -> list[tuple[str, str]]:
    tree = HTMLParser(document)
    output: list[tuple[str, str]] = []
    for anchor in tree.css("li.b_algo h2 a[href]"):
        url = str(anchor.attributes.get("href") or "").strip()
        title = clean_official_title(anchor.text(separator=" ", strip=True))
        if url.startswith(("http://", "https://")) and title:
            output.append((title, url))
    return output


def official_page_title(document: str) -> str:
    tree = HTMLParser(document)
    for selector in ("h1", ".article-title", ".title", "title"):
        node = tree.css_first(selector)
        if node is not None:
            title = clean_official_title(node.text(separator=" ", strip=True))
            if title:
                return title
    return ""


class GovLinkResolver:
    search_url = "https://cn.bing.com/search"

    def __init__(
        self, config_path: str | Path | None = None, cache_path: str | Path | None = None,
        request: Request | None = None,
    ) -> None:
        path = Path(config_path or os.getenv("GONGKAO_OFFICIAL_DOMAINS_CONFIG", "config/gongkao_official_domains.yaml"))
        config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.config = config if isinstance(config, Mapping) else {}
        self.policy = OfficialDomainPolicy(self.config)
        self.minimum_similarity = float(self.config.get("minimum_title_similarity") or 0.8)
        self.interval = float(self.config.get("search_interval_seconds") or 1.0)
        self.semaphore = asyncio.Semaphore(max(1, min(3, int(self.config.get("concurrency") or 3))))
        default_cache = Path("data/gongkao-link-resolver.db") if os.name == "nt" else Path("/var/lib/hot-gap/gongkao-link-resolver.db")
        self.cache = ResolverCache(
            cache_path or os.getenv("GONGKAO_LINK_CACHE") or default_cache,
            int(self.config.get("cache_ttl_hours") or 24),
        )
        self._request_override = request
        self._rate_lock = asyncio.Lock()
        self._last_search = 0.0
        self.stats = {"candidates": 0, "cached": 0, "searched": 0, "resolved": 0, "failed": 0}

    async def _request(self, url: str, **kwargs: Any) -> httpx.Response:
        if self._request_override is not None:
            return await self._request_override(url, **kwargs)
        async with httpx.AsyncClient(timeout=18.0, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"},
                **kwargs,
            )
            response.raise_for_status()
            return response

    async def _throttle(self) -> None:
        async with self._rate_lock:
            wait = self.interval - (time.monotonic() - self._last_search)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_search = time.monotonic()

    async def _resolve(self, source_title: str) -> tuple[str, str] | None:
        cached = self.cache.get(source_title)
        if cached is not None:
            self.stats["cached"] += 1
            if cached["status"] == "ok" and cached["result_url"]:
                return cached["result_title"], cached["result_url"]
            return None
        async with self.semaphore:
            try:
                await self._throttle()
                self.stats["searched"] += 1
                response = await self._request(self.search_url, params={"q": f'"{source_title}"'})
                for result_title, url in parse_bing_results(response.text):
                    if not self.policy.allows(url):
                        continue
                    if _title_similarity(source_title, result_title) < self.minimum_similarity:
                        continue
                    page_title = result_title
                    try:
                        page = await self._request(url)
                        extracted = official_page_title(page.text)
                        if extracted and _title_similarity(source_title, extracted) >= self.minimum_similarity:
                            page_title = extracted
                    except Exception:
                        pass
                    page_title = clean_official_title(page_title)
                    self.cache.put(source_title, url=url, result_title=page_title, status="ok")
                    self.stats["resolved"] += 1
                    return page_title, url
            except Exception as exc:
                LOGGER.warning("Official link lookup failed for %s: %s", source_title, exc)
            self.cache.put(source_title, status="not_found")
            self.stats["failed"] += 1
            return None

    @staticmethod
    def _mark_result(item: Item, result: tuple[str, str] | None) -> None:
        if result is None:
            item.extra["official_link_unresolved"] = True
            return
        official_title, official_url = result
        replaced = item.extra.get("replaced_urls")
        previous = list(replaced) if isinstance(replaced, list) else []
        item.extra["replaced_urls"] = list(dict.fromkeys([*previous, item.url]))
        item.url = official_url
        item.title = official_title
        item.title_zh = official_title
        item.extra.update({
            "source_site": "resolved", "subsource": "resolved",
            "announcement_url": official_url, "preferred_link_source": "resolved",
            "official_link_unresolved": False,
        })

    async def resolve_items(self, items: Iterable[Item]) -> dict[str, int]:
        groups: dict[str, list[Item]] = {}
        for item in items:
            if str(item.extra.get("source_site") or "").casefold() != "fenbi" or self.policy.allows(item.url):
                continue
            key = normalize_notice_title(item.title)
            if key:
                groups.setdefault(key, []).append(item)
        self.stats["candidates"] += sum(len(group) for group in groups.values())

        async def resolve_group(group: list[Item]) -> None:
            result = await self._resolve(group[0].title)
            for item in group:
                self._mark_result(item, result)

        await asyncio.gather(*(resolve_group(group) for group in groups.values()))
        return dict(self.stats)

    def close(self) -> None:
        self.cache.close()
