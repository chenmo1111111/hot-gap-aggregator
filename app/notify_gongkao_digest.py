"""Send one actionable Gongkao Top-10 card to a Feishu group each day."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

from app.pipeline.gongkao_classify import detail_category
from app.pipeline.gongkao_enrich import calculate_signup_status
from app.sync_feishu import CHINA_TZ, _coalesce, _load_items


LOGGER = logging.getLogger(__name__)
TARGET_PROVINCES = {"黑龙江", "辽宁", "河北", "天津", "山东"}


def item_key(row: Mapping[str, Any]) -> str:
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    return str(extra.get("id") or row.get("url") or "").strip()


def _candidate(row: Mapping[str, Any], today: date) -> dict[str, Any] | None:
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    end = _coalesce(row, "extra.endSignUpTime|endSignUpTime|报名截止|截止日期")
    status, days = calculate_signup_status(
        _coalesce(row, "extra.startSignUpTime|startSignUpTime|报名开始"), end, today=today
    )
    status = str(extra.get("signup_status") or status or "")
    days = extra.get("days_left") if extra.get("days_left") is not None else days
    if status not in {"报名中", "剩1天", "剩2天", "剩3天", "剩4天", "剩5天"}:
        return None
    if days is None:
        return None
    key = item_key(row)
    if not key:
        return None
    province = str(extra.get("province") or row.get("province") or "全国").removesuffix("省")
    title = str(row.get("title_zh") or row.get("title") or "未命名公告").strip()
    exam_type = str(extra.get("exam_type") or "")
    return {
        "key": key,
        "days": int(days),
        "province": province,
        "region": str(extra.get("region") or province or "全国"),
        "category": str(extra.get("detail_category") or detail_category(row)),
        "title": title,
        "url": str(_coalesce(
            row, "extra.apply_url|extra.signup_url|apply_url|signup_url|url"
        ) or "").strip(),
        "selection_priority": 0 if exam_type == "选调生" else 1,
        "province_priority": 0 if province in TARGET_PROVINCES else 1,
    }


def select_top10(
    rows: Iterable[Mapping[str, Any]], *, pushed: set[tuple[str, str]] | None = None,
    today: date | None = None, limit: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    current = today or datetime.now(CHINA_TZ).date()
    candidates = [item for row in rows if (item := _candidate(row, current))]
    candidates.sort(key=lambda item: (
        item["days"], item["selection_priority"], item["province_priority"], item["title"],
    ))
    seen = pushed or set()
    selected: list[dict[str, Any]] = []
    for item in candidates:
        first_seen = (item["key"], "first") in seen
        if not first_seen:
            item["push_bucket"] = "first"
        elif item["days"] <= 3 and (item["key"], "urgent") not in seen:
            item["push_bucket"] = "urgent"
        else:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected, len(candidates)


def _lark_text(value: object) -> str:
    return re.sub(r"[\[\]<>]", "", str(value or "")).replace("\n", " ").strip()


def build_card(
    items: list[Mapping[str, Any]], *, current_count: int, today: date,
    table_url: str, gongkao_new: int | None = None, qiuzhao_new: int | None = None,
) -> dict[str, Any]:
    title = f"【今日必做 · 公考】{today.isoformat()}　共 {current_count} 个报名中"
    lines = []
    for item in items:
        prefix = (
            f"**{item['days']}天** · {_lark_text(item['region'])} · "
            f"{_lark_text(item['category'])} · {_lark_text(item['title'])}"
        )
        link = str(item.get("url") or "")
        lines.append(f"{prefix}　[→ 报名]({link})" if link else prefix)
    content = "\n\n".join(lines) if lines else "今天没有新的待处理报名事项。"
    elements: list[dict[str, Any]] = []
    if gongkao_new is not None or qiuzhao_new is not None:
        elements.append({
            "tag": "div", "text": {"tag": "lark_md", "content": (
                f"今日新增：公考 **{gongkao_new or 0}** 条 · 秋招 **{qiuzhao_new or 0}** 条"
            )},
        })
    elements.extend([
        {"tag": "div", "text": {"tag": "lark_md", "content": content}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"[查看完整公考机会表]({table_url})"}},
    ])
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
            "elements": elements,
        },
    }


def _sign_payload(payload: dict[str, Any], secret: str) -> None:
    timestamp = str(int(datetime.now(CHINA_TZ).timestamp()))
    key = f"{timestamp}\n{secret}".encode("utf-8")
    payload["timestamp"] = timestamp
    payload["sign"] = base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()


class PushLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS push_log (
                item_key TEXT NOT NULL,
                reminder_bucket TEXT NOT NULL,
                pushed_at TEXT NOT NULL,
                PRIMARY KEY(item_key, reminder_bucket)
            );
            CREATE TABLE IF NOT EXISTS digest_runs (
                run_date TEXT PRIMARY KEY,
                pushed_at TEXT NOT NULL,
                item_count INTEGER NOT NULL
            );
        """)

    def already_sent_today(self, current: date) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM digest_runs WHERE run_date=?", (current.isoformat(),)
        ).fetchone() is not None

    def pushed(self) -> set[tuple[str, str]]:
        return {
            (str(row[0]), str(row[1]))
            for row in self.connection.execute("SELECT item_key,reminder_bucket FROM push_log")
        }

    def mark(self, items: Iterable[Mapping[str, Any]], current: date) -> None:
        now = datetime.now(CHINA_TZ).isoformat()
        rows = [(str(item["key"]), str(item["push_bucket"]), now) for item in items]
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO push_log(item_key,reminder_bucket,pushed_at) VALUES(?,?,?)", rows
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO digest_runs(run_date,pushed_at,item_count) VALUES(?,?,?)",
                (current.isoformat(), now, len(rows)),
            )

    def close(self) -> None:
        self.connection.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Send daily Gongkao Top-10 Feishu card")
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    parser.add_argument("--input", default="gongkao_enriched.json")
    parser.add_argument("--config", default=os.getenv("FEISHU_PUBLIC_SYNC_CONFIG", "config/feishu_public_sync.yaml"))
    parser.add_argument("--db", default=os.getenv("GONGKAO_DIGEST_DB", "/var/lib/hot-gap/gongkao-digest.db"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    webhook = os.getenv("FEISHU_DIGEST_WEBHOOK", "").strip()
    if not webhook:
        LOGGER.info("gongkao digest skipped: FEISHU_DIGEST_WEBHOOK is not configured")
        return 0
    current = datetime.now(CHINA_TZ).date()
    push_log = PushLog(args.db)
    try:
        if push_log.already_sent_today(current) and not args.force:
            LOGGER.info("gongkao digest skipped: already sent for %s", current)
            return 0
        rows = _load_items(Path(args.data_dir) / args.input)
        selected, current_count = select_top10(rows, pushed=push_log.pushed(), today=current)
        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        table_url = str(
            os.getenv("GONGKAO_PUBLIC_TABLE_URL")
            or config.get("gongkao_table_url")
            or ""
        ).strip()
        if not table_url:
            raise ValueError("GONGKAO_PUBLIC_TABLE_URL or gongkao_table_url is required")
        volume = {}
        try:
            volume_payload = json.loads((Path(args.data_dir) / "daily-volume.json").read_text(encoding="utf-8"))
            volume = next((row for row in reversed(volume_payload.get("history", [])) if row.get("date") == current.isoformat()), {})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        payload = build_card(
            selected, current_count=current_count, today=current, table_url=table_url,
            gongkao_new=volume.get("gongkao_new"), qiuzhao_new=volume.get("qiuzhao_new"),
        )
        secret = os.getenv("FEISHU_DIGEST_SIGN_SECRET", "").strip()
        if secret:
            _sign_payload(payload, secret)
        response = httpx.post(webhook, json=payload, timeout=15, follow_redirects=True)
        response.raise_for_status()
        body = response.json()
        if int(body.get("code", body.get("StatusCode", 0)) or 0) != 0:
            raise RuntimeError(f"Feishu webhook rejected card: {body}")
        push_log.mark(selected, current)
        LOGGER.info("gongkao digest sent: selected=%d current=%d", len(selected), current_count)
        return 0
    except Exception:
        LOGGER.exception("gongkao digest failed")
        return 1
    finally:
        push_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
