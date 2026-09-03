from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import yaml

from app.models import Item
from app.store.database import Database

LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


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
    exam_types_alert = set(raw.get("exam_types_alert", []) if isinstance(raw, dict) else [])
    cities_focus = [str(city).strip() for city in (raw.get("cities_focus", []) if isinstance(raw, dict) else []) if str(city).strip()]
    current = today or datetime.now(UTC).date()
    candidates: list[tuple[str, str]] = []
    for item in items:
        if item.source != "gongkao":
            continue
        province = str(item.extra.get("province") or "全国")
        exam_type = str(item.extra.get("exam_type") or "其他考试")
        haystack = f"{item.title} {item.title_zh or ''} {item.summary_zh or ''}".casefold()
        city_hits = [city for city in cities_focus if city.casefold() in haystack]
        if provinces and province not in provinces and exam_type not in exam_types_alert and not city_hits:
            continue
        item_id = str(item.extra.get("id") or item.url)
        subsource = str(item.extra.get("subsource") or item.extra.get("sub") or "")
        tag = "【选调预警】" if exam_type == "选调生" else "【国考公告】" if exam_type == "国考" or subsource == "scs" else "【公考提醒】"
        city_label = f"｜重点城市：{'、'.join(city_hits)}" if city_hits else ""
        suffix = f"{city_label}｜{item.url}｜{current.isoformat()}"
        if item.is_new and subsource in {"announcement", "scs"}:
            candidates.append((f"{item_id}:new", f"{tag}新公告｜{province}｜{item.title_zh or item.title}{suffix}"))
        for field, days, label in (
            ("startSignUpTime", 1, "明天开始报名"),
            ("endSignUpTime", 2, "距报名截止 2 天"),
            ("startWriteTime", 3, "距笔试 3 天"),
        ):
            event_date = _event_date(item.extra.get(field))
            if event_date and (event_date - current).days == days:
                candidates.append((f"{item_id}:{field}:{event_date.isoformat()}", f"{tag}{label}｜{province}｜{item.title_zh or item.title}{suffix}"))
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
    serverchan_key, feishu_webhook = os.getenv("SERVERCHAN_KEY"), os.getenv("FEISHU_WEBHOOK")
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
    if feishu_webhook:
        providers.append(("feishu", lambda: _post(feishu_webhook, json=_feishu_payload(text))))
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


async def notify_priority_alert(text: str, title: str = "关键期提醒") -> dict[str, str]:
    providers: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    bark_url, feishu_webhook = os.getenv("BARK_URL"), os.getenv("FEISHU_WEBHOOK")
    if bark_url:
        providers.append(("bark", lambda: _post(bark_url, json={"title": title, "body": text, "group": "hot-gap"})))
    if feishu_webhook:
        providers.append(("feishu", lambda: _post(feishu_webhook, json=_feishu_payload(text))))
    if not providers:
        LOGGER.info("priority notification skipped: no Bark or Feishu provider configured")
        return {}
    results = await asyncio.gather(*(sender() for _, sender in providers), return_exceptions=True)
    return {
        name: "degraded" if isinstance(result, Exception) else "ok"
        for (name, _), result in zip(providers, results, strict=True)
    }


async def notify_subsidy_alert(alert: dict[str, str]) -> dict[str, str]:
    """Send subsidy alerts to exactly one configured provider, in priority order."""
    feishu_webhook, bark_url = os.getenv("FEISHU_WEBHOOK"), os.getenv("BARK_URL")
    try:
        if feishu_webhook:
            await _post(feishu_webhook, json=_feishu_card_payload(alert))
            return {"feishu": "ok"}
        if bark_url:
            await _post(bark_url, json={
                "title": f"补贴预警·{alert.get('region', '')}",
                "body": alert.get("message", ""), "group": "hot-gap",
            })
            return {"bark": "ok"}
    except Exception as exc:
        provider = "feishu" if feishu_webhook else "bark"
        LOGGER.warning("subsidy notification provider %s failed: %s", provider, exc)
        return {provider: "degraded"}
    return {}


def _feishu_payload(text: str) -> dict[str, object]:
    payload: dict[str, object] = {"msg_type": "text", "content": {"text": text}}
    secret = os.getenv("FEISHU_SIGN_SECRET")
    if secret:
        timestamp = str(int(datetime.now(UTC).timestamp()))
        key = f"{timestamp}\n{secret}".encode("utf-8")
        signature = base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()
        payload.update({"timestamp": timestamp, "sign": signature})
    return payload


def _feishu_card_payload(alert: dict[str, str]) -> dict[str, object]:
    region = alert.get("region", "")
    title = alert.get("title", "补贴信息更新")
    detail = alert.get("summary") or f"{alert.get('type', '公告')} · {alert.get('date', '')}"
    payload: dict[str, object] = {
        "msg_type": "interactive",
        "card": {
            "header": {"template": "orange", "title": {"tag": "plain_text", "content": f"补贴预警 · {region}"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{detail}"}},
                {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "查看原文"}, "url": alert.get("url", ""), "type": "primary"}]},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": alert.get("created_at", "")}]},
            ],
        },
    }
    secret = os.getenv("FEISHU_SIGN_SECRET")
    if secret:
        timestamp = str(int(datetime.now(UTC).timestamp()))
        key = f"{timestamp}\n{secret}".encode("utf-8")
        signature = base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()
        payload.update({"timestamp": timestamp, "sign": signature})
    return payload


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
