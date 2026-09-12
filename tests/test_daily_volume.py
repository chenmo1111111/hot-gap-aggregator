from __future__ import annotations

import json
from datetime import date

from app.daily_volume import build_daily_broadcast, maybe_alert, update_daily_volume


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

    assert first["gongkao_new"] == 1 and first["qiuzhao_new"] == 2
    assert first["qiuzhao_wanqing_new"] == 1
    assert first["qiuzhao_xiaozhaoya_new"] == 1
    assert first["qiuzhao_other_new"] == 0
    assert first["qiuzhao_source_new"] == {
        "婉清购买表": 1, "校招鸭home一次性回填": 1,
    }
    assert first["gongkao_other_new"] == 1
    assert second["gongkao_new"] == 0 and second["qiuzhao_new"] == 0
    assert len(json.loads(output.read_text(encoding="utf-8"))["history"]) == 30


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

    assert first["gongkao_new"] == 1
    assert second["gongkao_new"] == 2
    assert second["gongkao_government_new"] == 1
    assert second["gongkao_sheet_new"] == 1


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

    assert result["qiuzhao_new"] == 2
    assert result["qiuzhao_source_new"] == {"婉清购买表": 1, "国聘": 1}
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

    assert maybe_alert(result, state_path=state, current_hour=9) is True
    assert maybe_alert(result, state_path=state, current_hour=9) is False
    message = sent[0]["json"]["content"]["text"]
    assert message == (
        "【每日采集播报】2026-09-11（当天累计，00:00-发送时）\n"
        "秋招：当天累计新增 30（婉清购买表 12 / 校招鸭home一次性回填 10 / 未拆分来源 8）\n"
        "公考：当天累计新增 8（政府源 5 / 校招鸭事业单位表 2 / 粉笔等补充源 1）"
    )

    low = {**result, "date": "2026-09-12", "qiuzhao_new": 19, "gongkao_new": 2}
    low_message = build_daily_broadcast(low)
    assert "低于20" in low_message
    assert "低于3" in low_message
