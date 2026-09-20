"""Capacity guard for the two 20,000-row Qiuzhao Feishu tables."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class CapacityReport:
    input_count: int
    reserved_rows: int
    output_count: int
    evicted_count: int
    evicted_oldest: str | None
    evicted_newest: str | None
    protected_recent_count: int
    unresolved_count: int
    triggered: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def qiuzhao_identity(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("source_record_id") or "").strip()
    if explicit:
        return explicit
    company = str(row.get("company_name") or row.get("company") or "").strip().casefold()
    position = str(row.get("position") or row.get("job") or row.get("title") or "").strip().casefold()
    return hashlib.sha256(f"{company}|{position}".encode("utf-8")).hexdigest() if company or position else ""


def _date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            stamp = int(value)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp).date()
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def load_first_seen(path: str | Path) -> dict[str, date]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    values = raw.get("qiuzhao_first_seen") if isinstance(raw, Mapping) else {}
    return {
        str(key): parsed
        for key, value in (values.items() if isinstance(values, Mapping) else [])
        if (parsed := _date(value)) is not None
    }


def row_first_seen(row: Mapping[str, Any], ledger: Mapping[str, date]) -> date | None:
    key = qiuzhao_identity(row)
    if key and key in ledger:
        return ledger[key]
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    return _date(
        row.get("first_seen") or extra.get("first_seen") or row.get("published_at")
        or row.get("updated_at")
    )


def enforce_qiuzhao_capacity(
    rows: list[Mapping[str, Any]], *, ledger: Mapping[str, date],
    today: date, reserved_rows: int = 0, trigger: int = 19_000,
    target: int = 18_500, hard_limit: int = 20_000, protect_days: int = 30,
) -> tuple[list[Mapping[str, Any]], CapacityReport]:
    if not (0 <= reserved_rows < hard_limit and 0 < target < trigger < hard_limit):
        raise ValueError("invalid Qiuzhao capacity policy")
    total = len(rows) + reserved_rows
    cutoff = today - timedelta(days=protect_days)
    dated = [(index, row_first_seen(row, ledger)) for index, row in enumerate(rows)]
    protected = {index for index, seen in dated if seen is None or seen >= cutoff}
    unresolved = sum(1 for _, seen in dated if seen is None)
    if total <= trigger:
        return rows, CapacityReport(
            len(rows), reserved_rows, len(rows), 0, None, None,
            len(protected), unresolved, False,
        )
    desired_rows = max(0, target - reserved_rows)
    needed = max(0, len(rows) - desired_rows)
    eligible = sorted(
        ((index, seen) for index, seen in dated if index not in protected and seen is not None),
        key=lambda item: (item[1], item[0]),
    )
    remove = eligible[:needed]
    remove_indexes = {index for index, _ in remove}
    kept = [row for index, row in enumerate(rows) if index not in remove_indexes]
    if len(kept) + reserved_rows >= hard_limit:
        raise RuntimeError(
            f"秋招表容量保护失败：受保护记录过多，预计 {len(kept) + reserved_rows} 行，"
            f"达到/超过硬上限 {hard_limit}"
        )
    dates = [seen for _, seen in remove]
    return kept, CapacityReport(
        len(rows), reserved_rows, len(kept), len(remove),
        min(dates).isoformat() if dates else None,
        max(dates).isoformat() if dates else None,
        len(protected), unresolved, True,
    )
