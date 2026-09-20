"""Capture recent Codefather recruitment rows from the public SSR listing."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping

import httpx
from dotenv import load_dotenv


LOGGER = logging.getLogger(__name__)
SOURCE_URL = "https://www.codefather.cn/job/recruitment"
SOURCE_LABEL = "编程导航"
UPSTREAM_SOURCE = "codefather"
CHINA_TZ = timezone(timedelta(hours=8))


class CaptureError(RuntimeError):
    """Raised when the public source no longer satisfies its contract."""


class _ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "script":
            self._in_script = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._in_script:
            self.scripts.append("".join(self._parts))
            self._in_script = False
            self._parts = []


def _json_array_after(text: str, marker: str) -> list[dict[str, Any]]:
    start = text.find(marker)
    if start < 0:
        raise CaptureError(f"Codefather SSR payload missing {marker}")
    start = text.find("[", start + len(marker))
    if start < 0:
        raise CaptureError("Codefather initialData is not an array")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, list):
        raise CaptureError("Codefather initialData is not a list")
    return [dict(row) for row in value if isinstance(row, Mapping)]


def parse_codefather_page(html: str) -> tuple[list[dict[str, Any]], int]:
    """Decode the Next.js flight payload; arrays are complete despite UI '+N'."""
    parser = _ScriptParser()
    parser.feed(html)
    chunks: list[str] = []
    for script in parser.scripts:
        prefix = "self.__next_f.push("
        if not script.startswith(prefix) or not script.endswith(")"):
            continue
        try:
            pushed = json.loads(script[len(prefix):-1])
        except json.JSONDecodeError:
            continue
        if isinstance(pushed, list) and len(pushed) >= 2 and isinstance(pushed[1], str):
            chunks.append(pushed[1])
    payload = "".join(chunks)
    rows = _json_array_after(payload, '"initialData":')
    total_match = re.search(r'"initialTotal":"?(\d+)"?', payload)
    if not total_match:
        raise CaptureError("Codefather SSR payload missing initialTotal")
    return rows, int(total_match.group(1))


def _timestamp(value: object) -> datetime | None:
    try:
        stamp = int(str(value or "").strip())
        if stamp > 10_000_000_000:
            stamp //= 1000
        return datetime.fromtimestamp(stamp, CHINA_TZ)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _unique_texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item or "").strip() for item in value if str(item or "").strip()))


def _company_type(industry: str) -> str:
    if "央企" in industry:
        return "央企"
    if "国企" in industry or "国央企" in industry:
        return "国企"
    if "外企" in industry or "外资" in industry:
        return "外企"
    if "银行" in industry:
        return "银行"
    return "其他"


def _industry_tag(industry: str) -> str:
    rules = (
        ("互联网/科技", ("互联网", "科技", "人工智能", "软件", "游戏", "电商", "通信")),
        ("金融银行", ("金融", "银行", "证券", "保险")),
        ("汽车新能源", ("汽车", "新能源")),
        ("生物医药", ("生物", "医药", "医疗")),
        ("半导体", ("半导体", "芯片", "集成电路")),
        ("制造业", ("制造", "机电", "电子", "电器", "化工")),
        ("快消零售", ("快消", "零售")),
        ("建筑", ("建筑", "地产")),
        ("能源", ("能源", "矿产", "矿业")),
        ("交通运输", ("交通", "物流", "运输")),
        ("研究所", ("研究所", "科研")),
    )
    return next((label for label, words in rules if any(word in industry for word in words)), "其他")


def normalize_codefather_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    record_id = str(row.get("id") or "").strip()
    company = str(row.get("companyName") or "").strip()
    positions = _unique_texts(row.get("positionList"))
    locations = _unique_texts(row.get("locationList"))
    created = _timestamp(row.get("createTime"))
    updated = _timestamp(row.get("updateTime"))
    if not record_id or not company or not positions or created is None:
        return None
    industry_raw = str(row.get("industry") or "").strip()
    batch = str(row.get("batch") or "").strip()
    cohort_match = re.search(r"(20\d{2}届)", batch)
    recruit_type = {1: "校招", 2: "社招", 3: "实习"}.get(row.get("recruitType"), "")
    apply_url = unescape(str(row.get("applyUrl") or "").strip())
    return {
        "source_record_id": f"codefather:{record_id}",
        "company_name": company,
        "company_type": _company_type(industry_raw),
        "industry": _industry_tag(industry_raw),
        "industry_raw": industry_raw,
        "position": "、".join(positions),
        "location": "、".join(locations),
        "cohort": cohort_match.group(1) if cohort_match else "",
        "recruitment_stage": batch or recruit_type,
        "deadline": "",
        "apply_url": apply_url,
        "announcement_url": apply_url,
        "published_at": created.date().isoformat(),
        "updated_at": (updated or created).isoformat(),
        "source_label": SOURCE_LABEL,
        "upstream_source": UPSTREAM_SOURCE,
        "extra": {
            "position_list": positions,
            "location_list": locations,
            "source_industry": industry_raw,
            "source_batch": batch,
            "source_recruit_type": recruit_type,
            "source_create_time": row.get("createTime"),
            "source_update_time": row.get("updateTime"),
        },
    }


def collect_recent(
    client: httpx.Client, *, today: date, days: int = 30, page_size: int = 50,
    max_pages: int = 151, stale_pages_to_stop: int = 3, delay_seconds: float = 0.15,
) -> dict[str, Any]:
    cutoff = today - timedelta(days=days - 1)
    collected: dict[str, dict[str, Any]] = {}
    source_total = 0
    scanned_pages = 0
    consecutive_stale = 0
    stopped_on_stale = False
    first_page_total = 0
    for current in range(1, max_pages + 1):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.get(SOURCE_URL, params={"current": current})
                response.raise_for_status()
                rows, page_total = parse_codefather_page(response.text)
                break
            except (httpx.HTTPError, CaptureError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
        else:
            raise CaptureError(f"Codefather page {current} failed after retries: {last_error}")
        scanned_pages += 1
        if current == 1:
            first_page_total = page_total
            max_pages = min(max_pages, max(1, math.ceil(page_total / page_size)))
        source_total = max(source_total, page_total)
        page_dates = [stamp.date() for row in rows if (stamp := _timestamp(row.get("createTime")))]
        fresh_count = 0
        for raw in rows:
            created = _timestamp(raw.get("createTime"))
            if created is None or created.date() < cutoff:
                continue
            normalized = normalize_codefather_row(raw)
            if normalized is not None:
                collected[normalized["source_record_id"]] = normalized
                fresh_count += 1
        if page_dates and max(page_dates) < cutoff:
            consecutive_stale += 1
        else:
            consecutive_stale = 0
        if consecutive_stale >= stale_pages_to_stop:
            stopped_on_stale = True
            break
        if not rows:
            break
        if delay_seconds:
            time.sleep(delay_seconds)
    items = sorted(
        collected.values(),
        key=lambda row: (str(row.get("published_at") or ""), str(row.get("source_record_id") or "")),
        reverse=True,
    )
    return {
        "generated_at": datetime.now(CHINA_TZ).isoformat(),
        "source": "qiuzhao",
        "status": {
            "status": "ok", "upstream_source": UPSTREAM_SOURCE,
            "source_label": SOURCE_LABEL, "item_count": len(items),
            "source_total_records": first_page_total or source_total,
            "scanned_pages": scanned_pages, "window_days": days,
            "window_start": cutoff.isoformat(), "window_end": today.isoformat(),
            "stopped_on_consecutive_stale_pages": stopped_on_stale,
            "stale_pages_to_stop": stale_pages_to_stop,
            "deduplicated_by": "source_record_id",
        },
        "items": items,
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def snapshot_is_fresh(path: Path, *, minimum_hours: float, now: datetime | None = None) -> bool:
    if minimum_hours <= 0 or not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(str(payload.get("generated_at") or "").replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=CHINA_TZ)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    current = now or datetime.now(CHINA_TZ)
    return timedelta(0) <= current - generated.astimezone(current.tzinfo) < timedelta(hours=minimum_hours)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Capture recent Codefather recruitment rows")
    parser.add_argument("--output", default="")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-pages", type=int, default=151)
    parser.add_argument("--today", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--min-refresh-hours", type=float,
        default=float(os.getenv("CODEFATHER_MIN_REFRESH_HOURS", "20") or 20),
    )
    arguments = parser.parse_args()
    today = date.fromisoformat(arguments.today) if arguments.today else datetime.now(CHINA_TZ).date()
    output = Path(arguments.output) if arguments.output else Path(
        os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data")
    ) / "qiuzhao_codefather.json"
    if not arguments.force and snapshot_is_fresh(output, minimum_hours=arguments.min_refresh_hours):
        print(json.dumps({
            "event": "codefather_capture_skipped", "reason": "snapshot_fresh",
            "minimum_hours": arguments.min_refresh_hours, "output": str(output),
        }, ensure_ascii=False))
        return 0
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HotGapAggregator/1.0; +https://hot.weixincuotiben.top)",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=45) as client:
            payload = collect_recent(
                client, today=today, days=max(1, arguments.days),
                max_pages=max(1, arguments.max_pages),
            )
        if not payload["items"]:
            raise CaptureError("Codefather 30-day capture returned zero rows; previous snapshot preserved")
        _atomic_write(output, payload)
        print(json.dumps({"event": "codefather_captured", **payload["status"], "output": str(output)}, ensure_ascii=False))
        return 0
    except Exception:
        LOGGER.exception("Codefather capture failed; previous snapshot preserved")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
