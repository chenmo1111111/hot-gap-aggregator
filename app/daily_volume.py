"""Track first-observed Gongkao/Qiuzhao volume and broadcast daily intake."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx
from dotenv import load_dotenv

CHINA_TZ = timezone(timedelta(hours=8))
SOURCE_ORDER = (
    "婉清购买表",
    "校招鸭home一次性回填",
    "国聘",
    "国家大学生就业服务平台",
    "教育部人才服务网",
    "应届生求职网",
    "高校就业网",
    "岗位雷达·腾讯",
    "岗位雷达·字节",
    "牛客网",
)


def _read_items(path: Path) -> list[Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("items") if isinstance(payload, Mapping) else payload
    return [row for row in (rows or []) if isinstance(row, Mapping)]


def _date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            stamp = int(value)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, CHINA_TZ).date()
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _extra(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("extra")
    return value if isinstance(value, Mapping) else {}


def _gongkao_id(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    return str(extra.get("id") or row.get("url") or "").strip()


def _qiuzhao_id(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("source_record_id") or "").strip()
    if explicit:
        return explicit
    value = "|".join(str(row.get(name) or "").strip().casefold() for name in ("company_name", "position"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value != "|" else ""


def _qiuzhao_source(row: Mapping[str, Any]) -> str:
    source = str(row.get("upstream_source") or "").strip().casefold()
    label = str(row.get("source_label") or "").strip().casefold()
    record_id = str(row.get("source_record_id") or "").strip().casefold()
    if "wanqing" in source or "婉清" in label:
        return "婉清购买表"
    if "xiaozhaoya" in source or record_id.startswith("xiaozhaoya:") or "校招鸭" in label:
        return "校招鸭home一次性回填"
    labels = {
        "国聘": "国聘",
        "国家大学生就业服务平台": "国家大学生就业服务平台",
        "教育部人才服务网": "教育部人才服务网",
        "应届生": "应届生求职网",
        "应届生求职网": "应届生求职网",
        "高校就业网": "高校就业网",
        "大厂雷达·腾讯": "岗位雷达·腾讯",
        "大厂雷达·字节": "岗位雷达·字节",
        "牛客网": "牛客网",
    }
    raw_label = str(row.get("source_label") or "").strip()
    if raw_label in labels:
        return labels[raw_label]
    company = str(row.get("company_name") or "").strip()
    if company == "腾讯":
        return "岗位雷达·腾讯"
    if company == "字节跳动":
        return "岗位雷达·字节"
    return raw_label or source or "未标注来源"


def _gongkao_source(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    upstream = str(extra.get("upstream_source") or "").strip().casefold()
    subsource = str(extra.get("subsource") or "").strip().casefold()
    source_site = str(extra.get("source_site") or "").strip().casefold()
    if upstream == "feishu_sheet" or subsource == "feishu_sheet":
        return "sheet"
    if (
        extra.get("government_source")
        or subsource == "government"
        or source_site in {"gov", "government"}
    ):
        return "government"
    return "other"


def _load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except (OSError, ValueError, json.JSONDecodeError):
        return fallback


def _atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def previous_day_window(run_at: datetime) -> tuple[datetime, datetime]:
    """Return the previous complete China-calendar day as a half-open window."""
    local = run_at.astimezone(CHINA_TZ)
    end = datetime.combine(local.date(), time.min, tzinfo=CHINA_TZ)
    return end - timedelta(days=1), end


def update_daily_volume(
    gongkao: list[Mapping[str, Any]], qiuzhao: list[Mapping[str, Any]], *,
    today: date, state_path: Path, output_path: Path, report_date: date | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    report_date = report_date or today
    state = _load_json(
        state_path,
        {"alerts_sent": []},
    )
    gongkao_initialized = "gongkao_first_seen" in state and isinstance(
        state.get("gongkao_first_seen"), dict
    )
    gongkao_seen = state.setdefault("gongkao_first_seen", {})
    if not isinstance(gongkao_seen, dict):
        gongkao_seen = state["gongkao_first_seen"] = {}
        gongkao_initialized = False
    gongkao_sources = state.setdefault("gongkao_first_seen_source", {})
    if not isinstance(gongkao_sources, dict):
        gongkao_sources = state["gongkao_first_seen_source"] = {}
    for row in gongkao:
        key = _gongkao_id(row)
        if not key:
            continue
        if key not in gongkao_seen:
            source_date = _date(_extra(row).get("first_seen") or row.get("published_at"))
            gongkao_seen[key] = (
                today if gongkao_initialized else (source_date or today - timedelta(days=1))
            ).isoformat()
        gongkao_sources.setdefault(key, _gongkao_source(row))
    qiuzhao_initialized = "qiuzhao_first_seen" in state and isinstance(
        state.get("qiuzhao_first_seen"), dict
    )
    qiuzhao_seen = state.setdefault("qiuzhao_first_seen", {})
    if not isinstance(qiuzhao_seen, dict):
        qiuzhao_seen = state["qiuzhao_first_seen"] = {}
    qiuzhao_sources = state.setdefault("qiuzhao_first_seen_source", {})
    if not isinstance(qiuzhao_sources, dict):
        qiuzhao_sources = state["qiuzhao_first_seen_source"] = {}
    for row in qiuzhao:
        key = _qiuzhao_id(row)
        if not key:
            continue
        if key not in qiuzhao_seen:
            source_date = _date(
                row.get("first_seen") or row.get("published_at") or row.get("updated_at")
                or _extra(row).get("first_seen")
            )
            qiuzhao_seen[key] = (
                today if qiuzhao_initialized else (source_date or today)
            ).isoformat()
        current_source = _qiuzhao_source(row)
        if qiuzhao_sources.get(key) in {None, "", "wanqing", "xiaozhaoya", "other"}:
            qiuzhao_sources[key] = current_source

    gongkao_report_keys = [
        key for key, value in gongkao_seen.items() if _date(value) == report_date
    ]
    active_qiuzhao_keys = {_qiuzhao_id(row) for row in qiuzhao}
    # The pre-source-breakdown state used ``other`` (or no value at all) and
    # cannot be attributed after the corresponding row has left the current
    # export.  Do not keep those one-time migration ghosts in today's totals:
    # every newly observed row now records its concrete collector immediately.
    legacy_orphans = {
        key for key, value in qiuzhao_seen.items()
        if _date(value) == today
        and key not in active_qiuzhao_keys
        and qiuzhao_sources.get(key) in {None, "", "other", "未标注来源"}
    }
    for key in legacy_orphans:
        qiuzhao_seen.pop(key, None)
        qiuzhao_sources.pop(key, None)
    qiuzhao_report_keys = [
        key for key, value in qiuzhao_seen.items() if _date(value) == report_date
    ]
    active_qiuzhao_sources = {_qiuzhao_source(row) for row in qiuzhao}
    active_qiuzhao_sources.update(
        qiuzhao_sources.get(key, "未标注来源") for key in qiuzhao_report_keys
    )
    qiuzhao_counts = {
        source: sum(
            qiuzhao_sources.get(key, "未标注来源") == source
            for key in qiuzhao_report_keys
        )
        for source in sorted(
            active_qiuzhao_sources,
            key=lambda value: (SOURCE_ORDER.index(value) if value in SOURCE_ORDER else len(SOURCE_ORDER), value),
        )
    }
    gongkao_counts = {
        source: sum(gongkao_sources.get(key, "other") == source for key in gongkao_report_keys)
        for source in ("government", "sheet", "other")
    }
    gongkao_new = len(gongkao_report_keys)
    qiuzhao_new = len(qiuzhao_report_keys)
    output = _load_json(output_path, {"history": []})
    replaced_dates = {report_date.isoformat()}
    if report_date != today:
        # Remove the old implementation's partial current-day row during the
        # transition to completed-day reporting.
        replaced_dates.add(today.isoformat())
    history = [
        entry for entry in output.get("history", [])
        if isinstance(entry, dict) and entry.get("date") not in replaced_dates
    ]
    window_start = datetime.combine(report_date, time.min, tzinfo=CHINA_TZ)
    window_end = window_start + timedelta(days=1)
    recorded = (recorded_at or datetime.now().astimezone()).isoformat()
    entry = {
        "date": report_date.isoformat(), "gongkao_new": gongkao_new,
        "qiuzhao_new": qiuzhao_new, "recorded_at": recorded,
        "period": f"{report_date.isoformat()} 00:00-{window_end.date().isoformat()} 00:00",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "qiuzhao_source_new": qiuzhao_counts,
        "qiuzhao_wanqing_new": qiuzhao_counts.get("婉清购买表", 0),
        "qiuzhao_xiaozhaoya_new": qiuzhao_counts.get("校招鸭home一次性回填", 0),
        "qiuzhao_other_new": sum(
            count for source, count in qiuzhao_counts.items()
            if source not in {"婉清购买表", "校招鸭home一次性回填"}
        ),
        "gongkao_government_new": gongkao_counts["government"],
        "gongkao_sheet_new": gongkao_counts["sheet"],
        "gongkao_other_new": gongkao_counts["other"],
    }
    history.append(entry)
    history = sorted(history, key=lambda row: str(row.get("date")))[-30:]
    _atomic(state_path, state)
    _atomic(output_path, {"generated_at": entry["recorded_at"], "history": history})
    return {**entry, "history_days": len(history)}


def build_daily_broadcast(result: Mapping[str, Any]) -> str:
    qiuzhao_note = "；低于20，请检查采集源" if int(result["qiuzhao_new"]) < 20 else ""
    gongkao_note = (
        "；低于3，事业编淡季可能正常，连续5天为0再重点处理"
        if int(result["gongkao_new"]) < 3 else ""
    )
    source_counts = result.get("qiuzhao_source_new")
    if isinstance(source_counts, Mapping):
        source_detail = " / ".join(f"{name} {count}" for name, count in source_counts.items())
    else:
        source_detail = (
            f"婉清购买表 {result['qiuzhao_wanqing_new']} / "
            f"校招鸭home一次性回填 {result['qiuzhao_xiaozhaoya_new']} / "
            f"未拆分来源 {result['qiuzhao_other_new']}"
        )
    return "\n".join((
        f"【每日采集播报】昨日（{str(result['date'])[5:]}）采集汇总",
        (
            f"秋招：新增 {result['qiuzhao_new']}（{source_detail}）"
            f"{qiuzhao_note}"
        ),
        (
            f"公考：新增 {result['gongkao_new']}（政府源 {result['gongkao_government_new']} / "
            f"校招鸭事业单位表 {result['gongkao_sheet_new']} / "
            f"粉笔等补充源 {result['gongkao_other_new']}）"
            f"{gongkao_note}"
        ),
    ))


def maybe_alert(result: Mapping[str, Any], *, state_path: Path, current_hour: int) -> bool:
    load_dotenv()
    if current_hour < int(os.getenv("DAILY_VOLUME_ALERT_AFTER_HOUR", "7")):
        return False
    day = str(result["date"])
    alert_key = f"previous-day:{day}"
    state = _load_json(state_path, {"alerts_sent": []})
    sent = set(str(value) for value in state.get("alerts_sent", []))
    if alert_key in sent:
        return False
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return False
    response = httpx.post(
        webhook,
        json={"msg_type": "text", "content": {"text": build_daily_broadcast(result)}},
        timeout=10, follow_redirects=True,
    )
    response.raise_for_status()
    # Version the key so an alert sent under the former partial-current-day
    # meaning cannot suppress the corrected complete-previous-day report.
    state["alerts_sent"] = sorted((*sent, alert_key))[-30:]
    _atomic(state_path, state)
    return True


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    parser.add_argument("--state", default=os.getenv("DAILY_VOLUME_STATE", "/var/lib/hot-gap/daily-volume-state.json"))
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    now = datetime.now().astimezone()
    window_start, _window_end = previous_day_window(now)
    result = update_daily_volume(
        _read_items(data_dir / "gongkao_enriched.json"),
        _read_items(data_dir / "qiuzhao.json"),
        today=now.date(), report_date=window_start.date(), recorded_at=now,
        state_path=Path(args.state), output_path=data_dir / "daily-volume.json",
    )
    result["alert_sent"] = maybe_alert(result, state_path=Path(args.state), current_hour=now.hour)
    print(json.dumps({"event": "daily_volume", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
