from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from app.daily_volume import (
    CHINA_TZ,
    build_daily_broadcast,
    maybe_alert,
    previous_day_window,
    repair_wanqing_first_seen,
    repair_wanqing_gongkao_first_seen,
    update_daily_volume,
)


def test_next_morning_wanqing_gongkao_route_separates_capture_and_source_day(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {}, "gongkao_first_seen_source": {},
        "qiuzhao_first_seen": {}, "qiuzhao_first_seen_source": {}, "alerts_sent": [],
    }), encoding="utf-8")
    row = {
        "url": "https://example.test/research",
        "published_at": "2026-09-13",
        "extra": {
            "id": "purchased:wanqing_feishu:research",
            "upstream_source": "wanqing_feishu",
        },
    }

    result = update_daily_volume(
        [row], [], today=date(2026, 9, 14), report_date=date(2026, 9, 13),
        state_path=state, output_path=output,
    )

    assert result["gongkao_captured_new"] == 0
    assert result["gongkao_new"] == 0  # no title: not a public-table row
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["gongkao_first_seen"]["purchased:wanqing_feishu:research"] == "2026-09-13"


def test_repair_wanqing_gongkao_date_preserves_other_sources(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {
            "purchased:wanqing_feishu:a": "2026-09-14",
            "government:b": "2026-09-14",
        },
        "gongkao_first_seen_source": {
            "purchased:wanqing_feishu:a": "婉清购买表分流",
            "government:b": "政府网站",
        },
        "qiuzhao_first_seen": {}, "qiuzhao_first_seen_source": {}, "alerts_sent": [],
    }), encoding="utf-8")

    result = repair_wanqing_gongkao_first_seen(
        [], state_path=state, mistaken_date=date(2026, 9, 14),
        date_overrides={"purchased:wanqing_feishu:a": date(2026, 9, 13)},
    )

    assert result["corrected_from_overrides"] == 1
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["gongkao_first_seen"]["purchased:wanqing_feishu:a"] == "2026-09-13"
    assert saved["gongkao_first_seen"]["government:b"] == "2026-09-14"


def test_daily_volume_tracks_first_seen_and_retains_thirty_days(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    output.write_text(json.dumps({"history": [
        {"date": f"2026-08-{day:02d}", "gongkao_new": 1, "qiuzhao_new": 1}
        for day in range(1, 31)
    ]}), encoding="utf-8")
    gongkao = [{
        "url": "https://gov.example/1", "published_at": "2026-09-10",
        "extra": {"id": "g1", "first_seen": "2026-09-10"},
    }]
    qiuzhao = [
        {
            "company_name": "公司甲", "position": "岗位甲", "source_record_id": "q1",
            "updated_at": "2026-09-10", "upstream_source": "wanqing_feishu",
        },
        {
            "company_name": "公司乙", "position": "岗位乙",
            "source_record_id": "xiaozhaoya:q2", "updated_at": "2026-09-10",
            "upstream_source": "xiaozhaoya",
        },
    ]

    first = update_daily_volume(
        gongkao, qiuzhao, today=date(2026, 9, 10), state_path=state, output_path=output,
    )
    second = update_daily_volume(
        gongkao, qiuzhao, today=date(2026, 9, 11), state_path=state, output_path=output,
    )

    assert first["gongkao_captured_new"] == 1 and first["qiuzhao_captured_new"] == 2
    assert first["qiuzhao_wanqing_new"] == 1
    assert first["qiuzhao_xiaozhaoya_new"] == 1
    assert first["qiuzhao_other_new"] == 0
    assert first["qiuzhao_source_new"]["婉清购买表"] == 1
    assert first["qiuzhao_source_new"]["校招鸭home一次性回填"] == 1
    assert first["gongkao_captured_source_new"]["其他来源"] == 1
    assert second["gongkao_captured_new"] == 0 and second["qiuzhao_captured_new"] == 0
    history = json.loads(output.read_text(encoding="utf-8"))["history"]
    assert len(history) == 30
    assert history[0]["count_basis"] == "legacy_intake_ledger"
    assert history[-1]["count_basis"] == "public_sync_candidates"


def test_daily_volume_accumulates_new_ids_across_refreshes_on_same_day(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {}, "gongkao_first_seen_source": {},
        "qiuzhao_first_seen": {}, "qiuzhao_first_seen_source": {}, "alerts_sent": [],
    }), encoding="utf-8")
    first_gongkao = {
        "url": "https://gov.example/1", "extra": {"id": "g1", "source_site": "gov"},
    }
    first = update_daily_volume(
        [first_gongkao], [], today=date(2026, 9, 12), state_path=state, output_path=output,
    )
    second = update_daily_volume(
        [{"url": "https://sheet.example/2", "extra": {"id": "g2", "upstream_source": "feishu_sheet"}}],
        [], today=date(2026, 9, 12), state_path=state, output_path=output,
    )

    assert first["gongkao_captured_new"] == 1
    assert second["gongkao_captured_new"] == 2
    assert second["gongkao_captured_source_new"]["政府网站"] == 1
    assert second["gongkao_captured_source_new"]["校招鸭事业单位购买表"] == 1
    assert second["gongkao_new"] == 0  # the historic ID is no longer in this snapshot


def test_broadcast_uses_public_candidates_not_ever_seen_ledger(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {"old-gov": "2026-09-16"},
        "gongkao_first_seen_source": {"old-gov": "政府网站"},
        "qiuzhao_first_seen": {"old-job": "2026-09-16"},
        "qiuzhao_first_seen_source": {"old-job": "国聘"},
        "alerts_sent": [],
    }), encoding="utf-8")
    gongkao = [
        {
            "title_zh": "2026年山东省事业单位公开招聘工作人员公告",
            "url": "https://hrss.shandong.gov.cn/art/2026/9/16/art_123_123.html",
            "published_at": "2026-09-16",
            "extra": {"id": "gov-1", "first_seen": "2026-09-16", "source_site": "gov", "province": "山东"},
        },
        {
            "title_zh": "2026年某地事业单位面试成绩公示",
            "url": "https://hrss.shandong.gov.cn/art/2026/9/16/art_123_124.html",
            "published_at": "2026-09-16",
            "extra": {"id": "noise-1", "first_seen": "2026-09-16", "source_site": "gov", "province": "山东"},
        },
        {
            "title_zh": "2027届中国移动校园招聘公告",
            "url": "https://www.10086.cn/campus/1", "published_at": "2026-09-16",
            "extra": {"id": "route-1", "first_seen": "2026-09-16", "source_site": "fenbi", "record_kind": "秋招"},
        },
    ]
    qiuzhao = [
        {"company_name": "公司甲", "position": "岗位甲", "source_record_id": "job-1",
         "updated_at": "2026-09-16", "upstream_source": "wanqing_feishu"},
        {"company_name": "公司甲", "position": "岗位甲", "source_record_id": "job-duplicate",
         "updated_at": "2026-09-16", "source_label": "国聘"},
    ]

    result = update_daily_volume(
        gongkao, qiuzhao, today=date(2026, 9, 17), report_date=date(2026, 9, 16),
        state_path=state, output_path=tmp_path / "daily-volume.json",
    )

    assert result["gongkao_captured_new"] == 1
    assert result["qiuzhao_captured_new"] == 1
    assert result["gongkao_new"] == 1
    assert result["qiuzhao_new"] == 2
    assert result["gongkao_source_new"]["政府网站"] == 1
    assert result["qiuzhao_source_new"]["婉清购买表"] == 1
    assert result["qiuzhao_source_new"]["公考源分流·粉笔"] == 1
    assert sum(result["gongkao_source_new"].values()) == result["gongkao_new"]
    assert sum(result["qiuzhao_source_new"].values()) == result["qiuzhao_new"]
    assert result["gongkao_public_stages"]["current_export"] == 3
    assert result["gongkao_public_stages"]["after_routing_filter"] == 1
    assert result["qiuzhao_public_stages"]["current_export"] == 2
    assert result["qiuzhao_public_stages"]["after_routing_merge"] == 2
    message = build_daily_broadcast(result)
    assert "秋招·昨日首次抓到 1 条" in message
    assert "秋招·源发布日期为昨日 2 条" in message
    assert "公考·昨日首次抓到 1 条" in message
    assert "公考·源发布日期为昨日 1 条" in message


def test_wanqing_full_snapshot_uses_source_dates_instead_of_import_day(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {}, "gongkao_first_seen_source": {},
        "qiuzhao_first_seen": {"existing": "2026-09-11"},
        "qiuzhao_first_seen_source": {"existing": "婉清购买表"},
        "alerts_sent": [],
    }), encoding="utf-8")
    bulk_snapshot = [
        {
            "source_record_id": "old-by-row-first-seen", "upstream_source": "wanqing_feishu",
            "first_seen": "2026-08-20", "updated_at": "2026-09-12",
        },
        {
            "source_record_id": "old-by-extra-first-seen", "upstream_source": "wanqing_feishu",
            "updated_at": "2026-09-12", "extra": {"first_seen": "2026-08-25"},
        },
        {
            "source_record_id": "old-by-published", "upstream_source": "wanqing_feishu",
            "published_at": "2026-09-01", "updated_at": "2026-09-12",
        },
        {
            "source_record_id": "old-by-updated", "upstream_source": "wanqing_feishu",
            "updated_at": 1788105600000,
        },
        {
            "source_record_id": "truly-undated-new", "upstream_source": "wanqing_feishu",
        },
    ]

    result = update_daily_volume(
        [], bulk_snapshot, today=date(2026, 9, 12),
        state_path=state, output_path=output,
    )

    assert result["qiuzhao_captured_new"] == 5
    assert result["qiuzhao_captured_source_new"]["婉清购买表"] == 5
    assert result["qiuzhao_new"] == 0  # rows lack company/position
    saved = json.loads(state.read_text(encoding="utf-8"))["qiuzhao_first_seen"]
    assert saved["old-by-row-first-seen"] == "2026-08-20"
    assert saved["old-by-extra-first-seen"] == "2026-08-25"
    assert saved["old-by-published"] == "2026-09-01"
    assert saved["old-by-updated"] == "2026-08-31"
    assert saved["truly-undated-new"] == "2026-09-12"


def test_next_morning_wanqing_capture_counts_rows_on_their_source_update_day(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {}, "gongkao_first_seen_source": {},
        "qiuzhao_first_seen": {}, "qiuzhao_first_seen_source": {},
        "alerts_sent": [],
    }), encoding="utf-8")
    row = {
        "source_record_id": "captured-next-morning",
        "upstream_source": "wanqing_feishu",
        "company_name": "次日抓取公司", "position": "研发岗",
        "updated_at": 1789142400000,
    }

    result = update_daily_volume(
        [], [row], today=date(2026, 9, 13), report_date=date(2026, 9, 12),
        state_path=state, output_path=output,
    )

    assert result["qiuzhao_captured_new"] == 0
    assert result["qiuzhao_published_new"] == 1
    assert result["qiuzhao_published_source_new"]["婉清购买表"] == 1
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["qiuzhao_first_seen"]["captured-next-morning"] == "2026-09-12"


def test_capture_day_and_publication_day_are_reported_separately(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {}, "gongkao_first_seen_source": {},
        "qiuzhao_first_seen": {}, "qiuzhao_first_seen_source": {},
        "gongkao_first_captured": {}, "gongkao_first_captured_source": {},
        "qiuzhao_first_captured": {}, "qiuzhao_first_captured_source": {},
        "alerts_sent": [],
    }), encoding="utf-8")
    row = {
        "company_name": "较早发布公司", "position": "算法工程师",
        "source_record_id": "ncss:older", "source_label": "国家大学生就业服务平台",
        "published_source_label": "国家大学生就业服务平台",
        "published_at": "2026-09-20", "upstream_source": "jobs",
    }

    result = update_daily_volume(
        [], [row], today=date(2026, 9, 25), report_date=date(2026, 9, 25),
        state_path=state, output_path=tmp_path / "daily-volume.json",
    )

    assert result["qiuzhao_captured_new"] == 1
    assert result["qiuzhao_captured_source_new"]["国家大学生就业服务平台"] == 1
    assert result["qiuzhao_published_new"] == 0
    assert result["qiuzhao_published_source_new"]["国家大学生就业服务平台"] == 0
    message = build_daily_broadcast(result)
    assert "秋招·昨日首次抓到 1 条" in message
    assert "秋招·源发布日期为昨日 0 条" in message


def test_repair_wanqing_bulk_import_preserves_unrelated_history(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    original = {
        "gongkao_first_seen": {"g1": "2026-09-12"},
        "gongkao_first_seen_source": {"g1": "government"},
        "qiuzhao_first_seen": {
            "old-row": "2026-09-12",
            "old-override": "2026-09-12",
            "actual-day": "2026-09-12",
            "other-source": "2026-09-12",
            "older-history": "2026-09-10",
        },
        "qiuzhao_first_seen_source": {
            "old-row": "婉清购买表",
            "old-override": "婉清购买表",
            "actual-day": "婉清购买表",
            "other-source": "国聘",
            "older-history": "婉清购买表",
        },
        "alerts_sent": ["previous-day:2026-09-11"],
    }
    state.write_text(json.dumps(original), encoding="utf-8")
    current = [
        {
            "source_record_id": "old-row", "upstream_source": "wanqing_feishu",
            "updated_at": "2026-08-20",
        },
        {
            "source_record_id": "actual-day", "upstream_source": "wanqing_feishu",
            "updated_at": "2026-09-12",
        },
        {
            "source_record_id": "other-source", "source_label": "国聘",
            "updated_at": "2026-08-01",
        },
    ]

    repair = repair_wanqing_first_seen(
        current, state_path=state, mistaken_date=date(2026, 9, 12),
        date_overrides={"old-override": date(2026, 8, 31)},
    )
    result = update_daily_volume(
        [{"url": "g1", "extra": {"id": "g1", "source_site": "gov"}}],
        current, today=date(2026, 9, 13), report_date=date(2026, 9, 12),
        state_path=state, output_path=output,
    )

    assert repair == {
        "mistaken_date": "2026-09-12", "candidate_count": 3,
        "corrected_from_rows": 1, "corrected_from_overrides": 1,
        "already_source_dated": 1, "unresolved_count": 0, "unresolved_ids": [],
    }
    assert result["qiuzhao_captured_new"] == 2
    assert result["qiuzhao_captured_source_new"]["婉清购买表"] == 1
    assert result["qiuzhao_captured_source_new"]["国聘"] == 1
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["qiuzhao_first_seen"] == {
        "old-row": "2026-08-20",
        "old-override": "2026-08-31",
        "actual-day": "2026-09-12",
        "other-source": "2026-09-12",
        "older-history": "2026-09-10",
    }
    assert saved["gongkao_first_seen"] == original["gongkao_first_seen"]
    assert saved["gongkao_first_seen_source"] == {"g1": "政府网站"}
    assert saved["alerts_sent"] == original["alerts_sent"]


def test_daily_volume_discards_only_unattributed_legacy_orphans(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {}, "gongkao_first_seen_source": {},
        "qiuzhao_first_seen": {
            "ghost-missing": "2026-09-12",
            "ghost-other": "2026-09-12",
            "known-inactive": "2026-09-12",
        },
        "qiuzhao_first_seen_source": {
            "ghost-other": "other", "known-inactive": "国聘",
        },
        "alerts_sent": [],
    }), encoding="utf-8")
    current = [{
        "source_record_id": "current", "company_name": "公司甲", "position": "岗位甲",
        "upstream_source": "wanqing_feishu",
    }]

    result = update_daily_volume(
        [], current, today=date(2026, 9, 12), state_path=state, output_path=output,
    )

    assert result["qiuzhao_captured_new"] == 2
    assert result["qiuzhao_captured_source_new"]["婉清购买表"] == 1
    assert result["qiuzhao_captured_source_new"]["国聘"] == 1
    assert result["qiuzhao_new"] == 0  # no dated public-table row
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "ghost-missing" not in saved["qiuzhao_first_seen"]
    assert "ghost-other" not in saved["qiuzhao_first_seen"]
    assert saved["qiuzhao_first_seen"]["known-inactive"] == "2026-09-12"


def test_daily_broadcast_is_always_sent_once_and_includes_source_counts(monkeypatch, tmp_path) -> None:
    state = tmp_path / "state.json"
    sent: list[dict] = []

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

    def fake_post(url: str, **kwargs):
        sent.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setenv("FEISHU_WEBHOOK", "https://open.feishu.test/hook")
    monkeypatch.setattr("app.daily_volume.httpx.post", fake_post)
    result = {
        "date": "2026-09-11", "qiuzhao_new": 30, "gongkao_new": 8,
        "qiuzhao_wanqing_new": 12, "qiuzhao_xiaozhaoya_new": 10,
        "qiuzhao_other_new": 8, "gongkao_government_new": 5,
        "gongkao_sheet_new": 2, "gongkao_other_new": 1,
    }

    assert maybe_alert(result, state_path=state, current_hour=6) is False
    assert maybe_alert(result, state_path=state, current_hour=7) is True
    assert maybe_alert(result, state_path=state, current_hour=9) is False
    assert "previous-day:2026-09-11" in json.loads(state.read_text(encoding="utf-8"))["alerts_sent"]
    message = sent[0]["json"]["content"]["text"]
    assert message.startswith(
        "【每日采集播报】昨日（09-11）双口径汇总\n"
        "统计窗口：2026-09-11 00:00—2026-09-12 00:00（北京时间）"
    )
    assert "秋招·昨日首次抓到 30 条" in message
    assert "秋招·源发布日期为昨日 30 条" in message
    assert "公考·昨日首次抓到 8 条" in message
    assert "公考·源发布日期为昨日 8 条" in message
    assert "非飞书 API 回读" in message

    low = {**result, "date": "2026-09-12", "qiuzhao_new": 19, "gongkao_new": 2}
    low_message = build_daily_broadcast(low)
    assert "低于20" in low_message
    assert "低于3" in low_message


def test_daily_broadcast_names_each_gongkao_purchase_and_web_source() -> None:
    result = {
        "date": "2026-09-12", "qiuzhao_new": 201, "gongkao_new": 235,
        "qiuzhao_source_new": {"婉清购买表": 106, "国聘": 1},
        "qiuzhao_wanqing_new": 106, "qiuzhao_xiaozhaoya_new": 0,
        "qiuzhao_other_new": 95,
        "gongkao_source_new": {
            "校招鸭事业单位购买表": 29,
            "婉清购买表分流": 103,
            "校招鸭home一次性基础层": 42,
            "粉笔": 60,
            "中公": 1,
        },
        "gongkao_government_new": 0, "gongkao_sheet_new": 29,
        "gongkao_other_new": 206,
    }

    message = build_daily_broadcast(result)

    assert "校招鸭事业单位购买表 29" in message
    assert "婉清购买表分流 103" in message
    assert "校招鸭home一次性基础层 42" in message
    assert "粉笔 60" in message
    assert "中公 1" in message


def test_two_daily_reports_cover_adjacent_complete_days(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    state.write_text(json.dumps({
        "gongkao_first_seen": {}, "gongkao_first_seen_source": {},
        "qiuzhao_first_seen": {}, "qiuzhao_first_seen_source": {}, "alerts_sent": [],
    }), encoding="utf-8")
    first_item = {"url": "https://gov.example/1", "extra": {"id": "g1", "source_site": "gov"}}
    second_item = {"url": "https://gov.example/2", "extra": {"id": "g2", "source_site": "gov"}}

    # A daytime refresh records g1 on 09-12.  The 09-13 07:00 run reports the
    # completed 09-12 day while recording g2 as first seen on 09-13.
    update_daily_volume(
        [first_item], [], today=date(2026, 9, 12), report_date=date(2026, 9, 11),
        state_path=state, output_path=output,
    )
    first_report = update_daily_volume(
        [first_item, second_item], [], today=date(2026, 9, 13),
        report_date=date(2026, 9, 12), state_path=state, output_path=output,
    )
    second_report = update_daily_volume(
        [first_item, second_item], [], today=date(2026, 9, 14),
        report_date=date(2026, 9, 13), state_path=state, output_path=output,
    )

    first_start = datetime.fromisoformat(first_report["window_start"])
    first_end = datetime.fromisoformat(first_report["window_end"])
    second_start = datetime.fromisoformat(second_report["window_start"])
    second_end = datetime.fromisoformat(second_report["window_end"])
    assert first_end == second_start
    assert first_end - first_start == timedelta(hours=24)
    assert second_end - second_start == timedelta(hours=24)
    assert first_report["gongkao_captured_new"] == 1
    assert second_report["gongkao_captured_new"] == 1
    assert build_daily_broadcast(first_report).startswith(
        "【每日采集播报】昨日（09-12）双口径汇总"
    )
    history = json.loads(output.read_text(encoding="utf-8"))["history"]
    assert [entry["date"] for entry in history][-2:] == ["2026-09-12", "2026-09-13"]


def test_legacy_partial_day_alert_does_not_suppress_complete_day_report(
    monkeypatch, tmp_path
) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"alerts_sent": ["2026-09-12"]}), encoding="utf-8")
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://open.feishu.test/hook")

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

    sent: list[dict] = []
    monkeypatch.setattr(
        "app.daily_volume.httpx.post",
        lambda _url, **kwargs: sent.append(kwargs) or Response(),
    )
    result = {
        "date": "2026-09-12", "qiuzhao_new": 0, "gongkao_new": 0,
        "qiuzhao_wanqing_new": 0, "qiuzhao_xiaozhaoya_new": 0,
        "qiuzhao_other_new": 0, "gongkao_government_new": 0,
        "gongkao_sheet_new": 0, "gongkao_other_new": 0,
    }

    assert maybe_alert(result, state_path=state, current_hour=7) is True
    assert len(sent) == 1


def test_previous_day_window_and_server_cron_are_both_seven_oclock() -> None:
    first = datetime(2026, 9, 13, 7, 0, tzinfo=CHINA_TZ)
    second = first + timedelta(days=1)
    first_start, first_end = previous_day_window(first)
    second_start, second_end = previous_day_window(second)
    assert first_start == datetime(2026, 9, 12, 0, 0, tzinfo=CHINA_TZ)
    assert first_end == second_start
    assert second_end - first_start == timedelta(hours=48)

    cron = (Path(__file__).parents[1] / "deploy/server/hot-gap-daily-volume.cron").read_text(
        encoding="utf-8"
    )
    schedule = next(line for line in cron.splitlines() if line.startswith("0 7 "))
    assert "DAILY_VOLUME_ALERT_AFTER_HOUR=7" in schedule
    assert "-m app.daily_volume" in schedule
    assert "GONGKAO_DIGEST_AFTER_HOUR=7" in schedule
    assert "-m app.notify_gongkao_digest" in schedule
