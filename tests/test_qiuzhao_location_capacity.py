from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.pipeline.qiuzhao_capacity import enforce_qiuzhao_capacity, qiuzhao_identity
from app.pipeline.qiuzhao_location import split_work_locations


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("北京、上海/深圳", ["北京", "上海", "深圳"]),
        ("北京市·上海市", ["北京", "上海"]),
        ("北京-海淀区", ["北京"]),
        ("全国各地", ["全国"]),
        ("详见附件", []),
    ],
)
def test_split_work_locations(raw: str, expected: list[str]) -> None:
    assert split_work_locations(raw) == expected


def _row(index: int) -> dict[str, str]:
    return {"company_name": f"公司{index}", "position": f"岗位{index}"}


def test_capacity_guard_evicts_oldest_and_protects_recent() -> None:
    today = date(2026, 9, 20)
    rows = [_row(index) for index in range(10)]
    ledger = {
        qiuzhao_identity(row): today - timedelta(days=100 - index * 10)
        for index, row in enumerate(rows)
    }
    kept, report = enforce_qiuzhao_capacity(
        rows, ledger=ledger, today=today, trigger=8, target=6, hard_limit=20,
        protect_days=30,
    )
    assert [row["company_name"] for row in kept] == [f"公司{i}" for i in range(4, 10)]
    assert report.evicted_count == 4
    assert report.evicted_oldest == "2026-06-12"
    assert report.evicted_newest == "2026-07-12"
    assert report.protected_recent_count == 3


def test_capacity_guard_hard_stops_when_protected_rows_fill_limit() -> None:
    today = date(2026, 9, 20)
    rows = [_row(index) for index in range(10)]
    ledger = {qiuzhao_identity(row): today for row in rows}
    with pytest.raises(RuntimeError, match="硬上限"):
        enforce_qiuzhao_capacity(
            rows, ledger=ledger, today=today, trigger=8, target=6,
            hard_limit=10, protect_days=30,
        )
