"""Report public-table daily volume while retaining the intake audit ledger."""

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
    "鲨鲨购买表",
    "国聘",
    "国家大学生就业服务平台",
    "教育部人才服务网",
    "应届生求职网",
    "高校就业网",
    "岗位雷达·腾讯",
    "岗位雷达·字节",
    "牛客网",
)
GONGKAO_SOURCE_ORDER = (
    "校招鸭事业单位购买表",
    "婉清购买表分流",
    "校招鸭home一次性基础层",
    "鲨鲨购买表分流",
    "政府网站",
    "粉笔",
    "中公",
    "其他来源",
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
    if "shasha" in source or record_id.startswith("shasha:") or "鲨鲨" in label:
        return "鲨鲨购买表"
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


def _qiuzhao_source_date(row: Mapping[str, Any]) -> date | None:
    """Prefer an explicit first-seen/publication date over mutable sync metadata."""
    extra = _extra(row)
    return _date(
        row.get("first_seen")
        or extra.get("first_seen")
        or row.get("published_at")
        or row.get("updated_at")
    )


def _gongkao_source(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    upstream = str(extra.get("upstream_source") or "").strip().casefold()
    subsource = str(extra.get("subsource") or "").strip().casefold()
    source_site = str(extra.get("source_site") or "").strip().casefold()
    if upstream == "feishu_sheet" or subsource == "feishu_sheet":
        return "校招鸭事业单位购买表"
    if upstream == "wanqing_feishu" or subsource == "wanqing_feishu":
        return "婉清购买表分流"
    if upstream == "xiaozhaoya" or subsource == "xiaozhaoya":
        return "校招鸭home一次性基础层"
    if upstream == "shasha_feishu" or subsource == "shasha_feishu":
        return "鲨鲨购买表分流"
    if (
        extra.get("government_source")
        or subsource == "government"
        or source_site in {"gov", "government"}
    ):
        return "政府网站"
    if source_site == "fenbi":
        return "粉笔"
    if source_site == "offcn" or subsource == "offcn":
        return "中公"
    return "其他来源"


def _gongkao_source_date(row: Mapping[str, Any]) -> date | None:
    """Return the source date used when a purchased row arrives the next morning."""
    extra = _extra(row)
    purchased = _gongkao_source(row) in {
        "校招鸭事业单位购买表", "婉清购买表分流", "校招鸭home一次性基础层", "鲨鲨购买表分流",
    }
    if purchased:
        value = row.get("published_at") or extra.get("source_first_seen") or extra.get("first_seen")
    else:
        value = extra.get("first_seen") or row.get("published_at")
    return _date(value or extra.get("issueTime") or extra.get("updateTime"))


def _normalized_gongkao_source(key: str, value: object) -> str:
    source = str(value or "").strip()
    aliases = {
        "sheet": "校招鸭事业单位购买表",
        "government": "政府网站",
        "other": "粉笔" if key.isdigit() else "其他来源",
    }
    return aliases.get(source, source or "其他来源")


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


def repair_wanqing_first_seen(
    qiuzhao: list[Mapping[str, Any]], *, state_path: Path, mistaken_date: date,
    date_overrides: Mapping[str, date] | None = None,
) -> dict[str, Any]:
    """Backdate one known Wanqing bulk-import day without touching other history."""
    state = _load_json(state_path, {"alerts_sent": []})
    seen = state.get("qiuzhao_first_seen")
    sources = state.get("qiuzhao_first_seen_source")
    if not isinstance(seen, dict) or not isinstance(sources, dict):
        raise ValueError("daily volume state has no initialized Qiuzhao first-seen maps")
    index = {_qiuzhao_id(row): row for row in qiuzhao if _qiuzhao_id(row)}
    overrides = dict(date_overrides or {})
    candidates = [
        key for key, value in seen.items()
        if _date(value) == mistaken_date and sources.get(key) == "婉清购买表"
    ]
    corrected_from_rows = 0
    corrected_from_overrides = 0
    already_source_dated = 0
    unresolved: list[str] = []
    for key in candidates:
        row = index.get(key)
        source_date = _qiuzhao_source_date(row) if row else None
        from_override = False
        if source_date is None and key in overrides:
            source_date = overrides[key]
            from_override = True
        if source_date is None or source_date > mistaken_date:
            unresolved.append(key)
            continue
        if source_date == mistaken_date:
            already_source_dated += 1
            continue
        seen[key] = source_date.isoformat()
        if from_override:
            corrected_from_overrides += 1
        else:
            corrected_from_rows += 1
    state["qiuzhao_first_seen_repairs"] = {
        **(
            state.get("qiuzhao_first_seen_repairs")
            if isinstance(state.get("qiuzhao_first_seen_repairs"), dict) else {}
        ),
        mistaken_date.isoformat(): {
            "source": "婉清购买表",
            "candidate_count": len(candidates),
            "corrected_from_rows": corrected_from_rows,
            "corrected_from_overrides": corrected_from_overrides,
            "already_source_dated": already_source_dated,
            "unresolved_count": len(unresolved),
        },
    }
    _atomic(state_path, state)
    return {
        "mistaken_date": mistaken_date.isoformat(),
        "candidate_count": len(candidates),
        "corrected_from_rows": corrected_from_rows,
        "corrected_from_overrides": corrected_from_overrides,
        "already_source_dated": already_source_dated,
        "unresolved_count": len(unresolved),
        "unresolved_ids": unresolved,
    }


def repair_wanqing_gongkao_first_seen(
    gongkao: list[Mapping[str, Any]], *, state_path: Path, mistaken_date: date,
    date_overrides: Mapping[str, date] | None = None,
) -> dict[str, Any]:
    """Backdate delayed Wanqing rows routed into Gongkao only."""
    state = _load_json(state_path, {"alerts_sent": []})
    seen = state.get("gongkao_first_seen")
    sources = state.get("gongkao_first_seen_source")
    if not isinstance(seen, dict) or not isinstance(sources, dict):
        raise ValueError("daily volume state has no initialized Gongkao first-seen maps")
    index = {_gongkao_id(row): row for row in gongkao if _gongkao_id(row)}
    overrides = dict(date_overrides or {})
    candidates = [
        key for key, value in seen.items()
        if _date(value) == mistaken_date
        and _normalized_gongkao_source(key, sources.get(key)) == "婉清购买表分流"
    ]
    corrected_from_rows = 0
    corrected_from_overrides = 0
    already_source_dated = 0
    unresolved: list[str] = []
    for key in candidates:
        row = index.get(key)
        source_date = _gongkao_source_date(row) if row else None
        from_override = False
        if key in overrides:
            source_date = overrides[key]
            from_override = True
        if source_date is None or source_date > mistaken_date:
            unresolved.append(key)
            continue
        if source_date == mistaken_date:
            already_source_dated += 1
            continue
        seen[key] = source_date.isoformat()
        if from_override:
            corrected_from_overrides += 1
        else:
            corrected_from_rows += 1
    state["gongkao_first_seen_repairs"] = {
        **(
            state.get("gongkao_first_seen_repairs")
            if isinstance(state.get("gongkao_first_seen_repairs"), dict) else {}
        ),
        mistaken_date.isoformat(): {
            "source": "婉清购买表分流",
            "candidate_count": len(candidates),
            "corrected_from_rows": corrected_from_rows,
            "corrected_from_overrides": corrected_from_overrides,
            "already_source_dated": already_source_dated,
            "unresolved_count": len(unresolved),
        },
    }
    _atomic(state_path, state)
    return {
        "mistaken_date": mistaken_date.isoformat(),
        "candidate_count": len(candidates),
        "corrected_from_rows": corrected_from_rows,
        "corrected_from_overrides": corrected_from_overrides,
        "already_source_dated": already_source_dated,
        "unresolved_count": len(unresolved),
        "unresolved_ids": unresolved,
    }


def previous_day_window(run_at: datetime) -> tuple[datetime, datetime]:
    """Return the previous complete China-calendar day as a half-open window."""
    local = run_at.astimezone(CHINA_TZ)
    end = datetime.combine(local.date(), time.min, tzinfo=CHINA_TZ)
    return end - timedelta(days=1), end


def _ordered_counts(counts: Mapping[str, int], order: tuple[str, ...]) -> dict[str, int]:
    """Keep even zero-volume configured sources visible in the daily breakdown."""
    return {
        source: counts.get(source, 0)
        for source in sorted(
            set(order) | set(counts),
            key=lambda value: (order.index(value) if value in order else len(order), value),
        )
    }


def _public_table_volume(
    gongkao: list[Mapping[str, Any]], qiuzhao: list[Mapping[str, Any]], *,
    report_date: date, today: date,
) -> dict[str, Any]:
    """Mirror public sync's routing, retention, mapping and business-key dedup.

    This describes the local *sync candidate* snapshot, not an API read-back of
    Feishu.  The public table can lag this snapshot when its sync has not run.
    """
    from app.pipeline.prune import filter_current_public_gongkao, load_retention
    from app.sync_feishu import _coalesce, date_to_millis, merge_qiuzhao_rows, partition_gongkao_rows
    from app.sync_feishu_public import (
        gongkao_key, map_public_gongkao, map_public_qiuzhao, qiuzhao_key,
    )

    def on_day(value: object) -> bool:
        return _date(value) == report_date

    raw_gongkao_day = sum(
        on_day(date_to_millis(_extra(row).get("first_seen"))) for row in gongkao
    )
    raw_qiuzhao_day = sum(
        on_day(date_to_millis(_coalesce(row, "updated_at|date|日期"))) for row in qiuzhao
    )
    gongkao_kept, routed, excluded = partition_gongkao_rows(gongkao)
    gongkao_current, _ = filter_current_public_gongkao(
        gongkao_kept, load_retention(), today=today,
    )
    qiuzhao_merged = merge_qiuzhao_rows(qiuzhao, routed)
    routed_sources = {
        _gongkao_id(row): _gongkao_source(row) for row in gongkao if _gongkao_id(row)
    }

    gongkao_by_key: dict[str, tuple[Mapping[str, Any], str]] = {}
    gongkao_invalid = 0
    for row in gongkao_current:
        try:
            fields = map_public_gongkao(row)
        except (TypeError, ValueError):
            if on_day(date_to_millis(_extra(row).get("first_seen"))):
                gongkao_invalid += 1
            continue
        key = gongkao_key(fields)
        if key:
            # diff_public_records uses the last mapped row for a shared URL.
            gongkao_by_key[key] = (fields, _gongkao_source(row))

    qiuzhao_by_key: dict[str, tuple[Mapping[str, Any], str]] = {}
    qiuzhao_invalid = 0
    for row in qiuzhao_merged:
        try:
            fields = map_public_qiuzhao(row)
        except (TypeError, ValueError):
            if on_day(date_to_millis(_coalesce(row, "updated_at|date|日期"))):
                qiuzhao_invalid += 1
            continue
        key = qiuzhao_key(fields)
        if not key:
            continue
        if str(row.get("upstream_source") or "") == "gongkao_routed":
            original_id = str(_extra(row).get("original_id") or "")
            source = f"公考源分流·{routed_sources.get(original_id, '其他来源')}"
        else:
            source = _qiuzhao_source(row)
        qiuzhao_by_key[key] = (fields, source)

    gongkao_counts: dict[str, int] = {}
    for fields, source in gongkao_by_key.values():
        if on_day(fields.get("首次收录")):
            gongkao_counts[source] = gongkao_counts.get(source, 0) + 1
    qiuzhao_counts: dict[str, int] = {}
    for fields, source in qiuzhao_by_key.values():
        if on_day(fields.get("日期")):
            qiuzhao_counts[source] = qiuzhao_counts.get(source, 0) + 1

    return {
        "gongkao_new": sum(gongkao_counts.values()),
        "qiuzhao_new": sum(qiuzhao_counts.values()),
        "gongkao_source_new": _ordered_counts(gongkao_counts, GONGKAO_SOURCE_ORDER),
        "qiuzhao_source_new": _ordered_counts(qiuzhao_counts, SOURCE_ORDER),
        "gongkao_public_stages": {
            "current_export": raw_gongkao_day,
            "after_routing_filter": sum(
                on_day(date_to_millis(_extra(row).get("first_seen"))) for row in gongkao_kept
            ),
            "after_retention": sum(
                on_day(date_to_millis(_extra(row).get("first_seen"))) for row in gongkao_current
            ),
            "invalid_mapping": gongkao_invalid,
            "candidate": sum(gongkao_counts.values()),
        },
        "qiuzhao_public_stages": {
            "current_export": raw_qiuzhao_day,
            "after_routing_merge": sum(
                on_day(date_to_millis(_coalesce(row, "updated_at|date|日期")))
                for row in qiuzhao_merged
            ),
            "invalid_mapping": qiuzhao_invalid,
            "candidate": sum(qiuzhao_counts.values()),
        },
        # These are global stage totals, deliberately not presented as a
        # previous-day exclusion breakdown.
        "public_pipeline_totals": {
            "gongkao_routed_all_dates": len(routed),
            "gongkao_excluded_all_dates": len(excluded),
        },
    }


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
            source_date = _gongkao_source_date(row)
            gongkao_seen[key] = (
                (source_date or today)
                if gongkao_initialized else (source_date or today - timedelta(days=1))
            ).isoformat()
        current_source = _gongkao_source(row)
        if gongkao_sources.get(key) in {None, "", "sheet", "government", "other"}:
            gongkao_sources[key] = current_source
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
            # A newly visible ID can be an old row revealed by a larger/full
            # purchased-table snapshot.  Use its source date when available so
            # a backfill is not reported as thousands of new jobs today.
            qiuzhao_seen[key] = (_qiuzhao_source_date(row) or today).isoformat()
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
    qiuzhao_counts_from_ledger = {
        source: sum(
            qiuzhao_sources.get(key, "未标注来源") == source
            for key in qiuzhao_report_keys
        )
        for source in sorted(
            active_qiuzhao_sources,
            key=lambda value: (SOURCE_ORDER.index(value) if value in SOURCE_ORDER else len(SOURCE_ORDER), value),
        )
    }
    active_gongkao_sources = {_gongkao_source(row) for row in gongkao}
    active_gongkao_sources.update(
        _normalized_gongkao_source(key, gongkao_sources.get(key))
        for key in gongkao_report_keys
    )
    gongkao_counts_from_ledger = {
        source: sum(
            _normalized_gongkao_source(key, gongkao_sources.get(key)) == source
            for key in gongkao_report_keys
        )
        for source in sorted(
            active_gongkao_sources,
            key=lambda value: (
                GONGKAO_SOURCE_ORDER.index(value)
                if value in GONGKAO_SOURCE_ORDER else len(GONGKAO_SOURCE_ORDER),
                value,
            ),
        )
    }
    # Keep the historic intake ledger for audit, but never present its
    # ever-observed IDs as rows newly visible in the public Feishu tables.
    public = _public_table_volume(
        gongkao, qiuzhao, report_date=report_date, today=today,
    )
    gongkao_counts = public["gongkao_source_new"]
    qiuzhao_counts = public["qiuzhao_source_new"]
    gongkao_new = public["gongkao_new"]
    qiuzhao_new = public["qiuzhao_new"]
    output = _load_json(output_path, {"history": []})
    replaced_dates = {report_date.isoformat()}
    if report_date != today:
        # Remove the old implementation's partial current-day row during the
        # transition to completed-day reporting.
        replaced_dates.add(today.isoformat())
    history = [
        {"count_basis": "legacy_intake_ledger", **entry}
        if "count_basis" not in entry else entry
        for entry in output.get("history", [])
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
        "count_basis": "public_sync_candidates",
        "snapshot_note": "按本地公开表同步规则计算；不是飞书 API 回读的已同步行数",
        "gongkao_captured_new": len(gongkao_report_keys),
        "qiuzhao_captured_new": len(qiuzhao_report_keys),
        "gongkao_captured_source_new": _ordered_counts(gongkao_counts_from_ledger, GONGKAO_SOURCE_ORDER),
        "qiuzhao_captured_source_new": _ordered_counts(qiuzhao_counts_from_ledger, SOURCE_ORDER),
        "gongkao_public_stages": public["gongkao_public_stages"],
        "qiuzhao_public_stages": public["qiuzhao_public_stages"],
        "qiuzhao_source_new": qiuzhao_counts,
        "qiuzhao_wanqing_new": qiuzhao_counts.get("婉清购买表", 0),
        "qiuzhao_xiaozhaoya_new": qiuzhao_counts.get("校招鸭home一次性回填", 0),
        "qiuzhao_other_new": sum(
            count for source, count in qiuzhao_counts.items()
            if source not in {"婉清购买表", "校招鸭home一次性回填"}
        ),
        "gongkao_source_new": gongkao_counts,
        "gongkao_government_new": gongkao_counts.get("政府网站", 0),
        "gongkao_sheet_new": gongkao_counts.get("校招鸭事业单位购买表", 0),
        "gongkao_other_new": sum(
            count for source, count in gongkao_counts.items()
            if source not in {"政府网站", "校招鸭事业单位购买表"}
        ),
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
    gongkao_source_counts = result.get("gongkao_source_new")
    if isinstance(gongkao_source_counts, Mapping):
        gongkao_source_detail = " / ".join(
            f"{name} {count}" for name, count in gongkao_source_counts.items()
        )
    else:
        gongkao_source_detail = (
            f"政府源 {result['gongkao_government_new']} / "
            f"校招鸭事业单位表 {result['gongkao_sheet_new']} / "
            f"粉笔等补充源 {result['gongkao_other_new']}"
        )
    day = date.fromisoformat(str(result["date"]))
    lines = [
        f"【每日采集播报】昨日（{day:%m-%d}）公开表汇总",
        f"统计窗口：{day:%Y-%m-%d} 00:00—{day + timedelta(days=1):%Y-%m-%d} 00:00（北京时间）",
        f"秋招：公开表候选 {result['qiuzhao_new']}{qiuzhao_note}",
        f"秋招来源：{source_detail}",
        f"公考：公开表候选 {result['gongkao_new']}{gongkao_note}",
        f"公考来源：{gongkao_source_detail}",
    ]
    qiuzhao_stages = result.get("qiuzhao_public_stages")
    if isinstance(qiuzhao_stages, Mapping):
        lines.append(
            "秋招核对：采集台账 "
            f"{result['qiuzhao_captured_new']}；当前导出昨日日期 "
            f"{qiuzhao_stages['current_export']}；合并公考分流 "
            f"{qiuzhao_stages['after_routing_merge']}；映射/查重后可入表 {result['qiuzhao_new']}"
        )
    gongkao_stages = result.get("gongkao_public_stages")
    if isinstance(gongkao_stages, Mapping):
        lines.append(
            "公考核对：采集台账 "
            f"{result['gongkao_captured_new']}；当前导出昨日首次收录 "
            f"{gongkao_stages['current_export']}；分类/去噪 "
            f"{gongkao_stages['after_routing_filter']}；留存 "
            f"{gongkao_stages['after_retention']}；映射/查重后可入表 {result['gongkao_new']}"
        )
    lines.append(
        "口径：按本地公开表同步规则计算的候选快照，非飞书 API 回读；"
        "台账和公开表的日期字段及范围不同，不能直接相减。"
    )
    return "\n".join(lines)


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
    parser.add_argument("--repair-wanqing-date")
    parser.add_argument("--repair-gongkao-wanqing-date")
    parser.add_argument("--report-date")
    parser.add_argument(
        "--repair-date-override", action="append", default=[], metavar="ID=YYYY-MM-DD",
    )
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    now = datetime.now().astimezone()
    window_start, _window_end = previous_day_window(now)
    gongkao_items = _read_items(data_dir / "gongkao_enriched.json")
    qiuzhao_items = _read_items(data_dir / "qiuzhao.json")
    repair_result = None
    gongkao_repair_result = None
    report_date = (
        date.fromisoformat(args.report_date)
        if args.report_date else window_start.date()
    )
    if args.repair_wanqing_date:
        repair_date = date.fromisoformat(args.repair_wanqing_date)
        if not args.report_date:
            report_date = repair_date
        overrides: dict[str, date] = {}
        for value in args.repair_date_override:
            key, separator, raw_date = value.partition("=")
            if not separator or not key.strip():
                parser.error("--repair-date-override must be ID=YYYY-MM-DD")
            overrides[key.strip()] = date.fromisoformat(raw_date.strip())
        repair_result = repair_wanqing_first_seen(
            qiuzhao_items, state_path=Path(args.state), mistaken_date=repair_date,
            date_overrides=overrides,
        )
    if args.repair_gongkao_wanqing_date:
        repair_date = date.fromisoformat(args.repair_gongkao_wanqing_date)
        if not args.report_date:
            report_date = repair_date
        overrides: dict[str, date] = {}
        for value in args.repair_date_override:
            key, separator, raw_date = value.partition("=")
            if not separator or not key.strip():
                parser.error("--repair-date-override must be ID=YYYY-MM-DD")
            overrides[key.strip()] = date.fromisoformat(raw_date.strip())
        gongkao_repair_result = repair_wanqing_gongkao_first_seen(
            gongkao_items, state_path=Path(args.state), mistaken_date=repair_date,
            date_overrides=overrides,
        )
    result = update_daily_volume(
        gongkao_items, qiuzhao_items,
        today=now.astimezone(CHINA_TZ).date(), report_date=report_date, recorded_at=now,
        state_path=Path(args.state), output_path=data_dir / "daily-volume.json",
    )
    if repair_result is not None:
        result["repair"] = repair_result
    if gongkao_repair_result is not None:
        result["gongkao_repair"] = gongkao_repair_result
    result["alert_sent"] = (
        False if args.no_alert
        else maybe_alert(
            result, state_path=Path(args.state),
            current_hour=now.astimezone(CHINA_TZ).hour,
        )
    )
    print(json.dumps({"event": "daily_volume", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
