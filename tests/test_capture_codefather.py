from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from app.capture_codefather import (
    CHINA_TZ,
    collect_recent,
    normalize_codefather_row,
    parse_codefather_page,
    snapshot_is_fresh,
)


FIXTURE = Path(__file__).parent / "fixtures" / "jobs" / "codefather_page1.html"


def test_real_codefather_flight_fixture_preserves_full_hidden_arrays() -> None:
    rows, total = parse_codefather_page(FIXTURE.read_text(encoding="utf-8"))
    assert total == 7514
    assert len(rows) == 3
    assert rows[0]["positionList"] == [
        "工艺工程师", "设备工程师", "良率提升工程师", "制程整合工程师",
        "智能制造工程师", "工艺整合研发工程师", "模组工艺研发工程师",
    ]
    assert rows[2]["locationList"] == ["南京", "深圳", "珠海"]


def test_normalize_codefather_row_keeps_complete_positions_locations_and_source_date() -> None:
    rows, _ = parse_codefather_page(FIXTURE.read_text(encoding="utf-8"))
    item = normalize_codefather_row(rows[2])
    assert item is not None
    assert item["source_record_id"] == "codefather:16660"
    assert item["position"] == "嵌入式软件工程师、算法工程师、硬件工程师、测试工程师"
    assert item["location"] == "南京、深圳、珠海"
    assert item["extra"]["position_list"] == rows[2]["positionList"]
    assert item["extra"]["location_list"] == rows[2]["locationList"]
    assert item["published_at"] == "2026-09-20"
    encoded = dict(rows[2], applyUrl="https://mp.weixin.qq.com/s/example?scene=1&amp;click_id=42")
    assert normalize_codefather_row(encoded)["apply_url"].endswith("?scene=1&click_id=42")


def test_collect_recent_filters_old_rows_deduplicates_ids_and_stops_after_stale_pages() -> None:
    fixture = FIXTURE.read_text(encoding="utf-8")
    recent_rows, _ = parse_codefather_page(fixture)
    old = dict(recent_rows[0], id="old", createTime=1770000000000, updateTime=1770000000000)

    def page(rows: list[dict], total: int = 7514) -> str:
        import json
        payload = f'f:["$","$L46",null,{{"initialData":{json.dumps(rows, ensure_ascii=False, separators=(",", ":"))},"initialTotal":"{total}"}}]\\n'
        return f'<script>self.__next_f.push({json.dumps([1, payload], ensure_ascii=False)})</script>'

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        current = int(request.url.params.get("current", "1"))
        calls.append(current)
        if current == 1:
            return httpx.Response(200, text=page(recent_rows))
        if current == 2:
            return httpx.Response(200, text=page([recent_rows[0], old]))
        return httpx.Response(200, text=page([old]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = collect_recent(
            client, today=date(2026, 9, 20), max_pages=10,
            stale_pages_to_stop=3, delay_seconds=0,
        )

    assert len(result["items"]) == 3
    assert calls == [1, 2, 3, 4, 5]
    assert result["status"]["stopped_on_consecutive_stale_pages"] is True


def test_snapshot_freshness_limits_three_hour_refresh_to_about_once_daily(tmp_path) -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=CHINA_TZ)
    path = tmp_path / "qiuzhao_codefather.json"
    path.write_text(json.dumps({"generated_at": (now - timedelta(hours=19)).isoformat()}), encoding="utf-8")
    assert snapshot_is_fresh(path, minimum_hours=20, now=now) is True
    path.write_text(json.dumps({"generated_at": (now - timedelta(hours=21)).isoformat()}), encoding="utf-8")
    assert snapshot_is_fresh(path, minimum_hours=20, now=now) is False
