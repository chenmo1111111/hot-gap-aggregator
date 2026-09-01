from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import yaml

from app.models import Item
from app.store.database import Database

LOGGER = logging.getLogger(__name__)


def build_top20(items: list[Item]) -> str:
    ranked = sorted(items, key=lambda item: (-item.cluster_size, item.rank, item.source))[:20]
    lines = ["信息差日报 · 今日 Top 20"]
    for index, item in enumerate(ranked, 1):
        summary = f" — {item.summary_zh}" if item.summary_zh else ""
        lines.append(f"{index}. [{item.source}] {item.title_zh or item.title}{summary}")
    return "\n".join(lines)[:3900]


def build_gongkao_events(
    items: list[Item], database: Database, today: date | None = None,
    config_path: str | Path | None = None,
) -> tuple[list[str], list[str]]:
    path = Path(config_path or os.getenv("GONGKAO_WATCH_CONFIG", "config/gongkao_watch.yaml"))
    if not path.exists():
        return [], []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    provinces = set(raw.get("provinces", raw) if isinstance(raw, dict) else raw)
    current = today or datetime.now(UTC).date()
    candidates: list[tuple[str, str]] = []
    for item in items:
        if item.source != "gongkao":
            continue
        province = str(item.extra.get("province") or "全国")
        if provinces and province not in provinces:
            continue
        item_id = str(item.extra.get("id") or item.url)
        if item.is_new and item.extra.get("sub") == "announcement":
            candidates.append((f"{item_id}:new", f"新公告｜{province}｜{item.title_zh or item.title}"))
        for field, days, label in (
            ("startSignUpTime", 1, "明天开始报名"),
            ("endSignUpTime", 2, "距报名截止 2 天"),
            ("startWriteTime", 3, "距笔试 3 天"),
        ):
            event_date = _event_date(item.extra.get(field))
            if event_date and (event_date - current).days == days:
                candidates.append((f"{item_id}:{field}:{event_date.isoformat()}", f"{label}｜{province}｜{item.title_zh or item.title}"))
    unseen = database.unseen_gongkao_events([key for key, _ in candidates])
    return [line for key, line in candidates if key in unseen], [key for key, _ in candidates if key in unseen]


async def notify_top20(items: list[Item], database: Database | None = None) -> dict[str, str]:
    text = build_top20(items)
    event_keys: list[str] = []
    if database:
        event_lines, event_keys = build_gongkao_events(items, database)
        if event_lines:
            text += "\n\n公考节点提醒\n" + "\n".join(f"• {line}" for line in event_lines)
            text = text[:3900]
    providers: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    bark_url = os.getenv("BARK_URL")
    telegram_token, telegram_chat = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID")
    serverchan_key = os.getenv("SERVERCHAN_KEY")
    if bark_url:
        providers.append(("bark", lambda: _post(bark_url, json={"title": "信息差日报", "body": text, "group": "hot-gap"})))
    if telegram_token and telegram_chat:
        providers.append(("telegram", lambda: _post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={"chat_id": telegram_chat, "text": text, "disable_web_page_preview": True},
        )))
    if serverchan_key:
        providers.append(("serverchan", lambda: _post(
            f"https://sctapi.ftqq.com/{serverchan_key}.send", data={"title": "信息差日报", "desp": text},
        )))
    if not providers:
        LOGGER.info("notification skipped: no provider configured")
        return {}
    results = await asyncio.gather(*(sender() for _, sender in providers), return_exceptions=True)
    status: dict[str, str] = {}
    for (name, _), result in zip(providers, results, strict=True):
        if isinstance(result, Exception):
            LOGGER.warning("notification provider %s failed: %s", name, result)
            status[name] = "degraded"
        else:
            status[name] = "ok"
    if database and event_keys and any(value == "ok" for value in status.values()):
        database.mark_gongkao_events(event_keys)
    return status


def _event_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            stamp = int(value)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, UTC).date()
        return date.fromisoformat(str(value)[:10])
    except (ValueError, OSError, OverflowError):
        return None


async def _post(url: str, **kwargs: object) -> None:
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for attempt in range(3):
            try:
                response = await client.post(url, **kwargs)
                response.raise_for_status()
                return
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"notification request failed: {last_error}")
