from __future__ import annotations

from datetime import date

from unittest.mock import Mock, call

from app.notify_gongkao_digest import (
    build_card, digest_webhooks, previous_public_volume, select_top10, send_digest,
)


def _row(identifier: str, title: str, days: int, *, province="广东", exam_type="事业单位"):
    return {
        "title": title,
        "url": f"https://example.test/{identifier}",
        "extra": {
            "id": identifier, "province": province, "exam_type": exam_type,
            "startSignUpTime": "2026-09-01", "endSignUpTime": f"2026-09-{7 + days:02d}",
        },
    }


def test_top10_sorts_deadline_then_selection_then_target_province_and_skips_seen() -> None:
    rows = [
        _row("normal", "普通", 4),
        _row("target", "目标省", 4, province="山东"),
        _row("selection", "选调", 4, exam_type="选调生"),
        _row("tomorrow", "明日截止", 1),
        _row("seen", "已推过", 5),
    ]
    selected, total = select_top10(
        rows, pushed={("seen", "first")}, today=date(2026, 9, 7)
    )
    assert total == 5
    assert [item["key"] for item in selected] == ["tomorrow", "selection", "target", "normal"]


def test_urgent_item_can_be_reminded_once() -> None:
    rows = [_row("urgent", "紧急", 2)]
    selected, _ = select_top10(rows, pushed={("urgent", "first")}, today=date(2026, 9, 7))
    assert selected[0]["push_bucket"] == "urgent"
    selected_again, _ = select_top10(
        rows, pushed={("urgent", "first"), ("urgent", "urgent")}, today=date(2026, 9, 7)
    )
    assert selected_again == []


def test_card_contains_title_rows_and_public_link() -> None:
    selected, total = select_top10([_row("one", "测试公告", 2)], today=date(2026, 9, 7))
    card = build_card(selected, current_count=total, today=date(2026, 9, 7), table_url="https://table.test")
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["title"]["content"] == "【今日必做 · 公考】2026-09-07　共 1 个报名中"
    body = card["card"]["elements"][0]["text"]["content"]
    assert "2天" in body and "测试公告" in body and "[→ 报名](https://example.test/one)" in body


def test_card_contains_daily_volume_when_supplied() -> None:
    selected, total = select_top10([_row("one", "测试公告", 2)], today=date(2026, 9, 7))
    card = build_card(
        selected, current_count=total, today=date(2026, 9, 7),
        table_url="https://table.test", gongkao_new=6, qiuzhao_new=35,
    )
    content = "\n".join(
        str(element.get("text", {}).get("content", "")) for element in card["card"]["elements"]
    )
    assert "公考 **6** 条" in content
    assert "秋招 **35** 条" in content
    assert "昨日（09-06）公开表候选" in content
    assert "今日新增" not in content


def test_digest_uses_only_previous_complete_public_report() -> None:
    payload = {"history": [
        {"date": "2026-09-16", "gongkao_new": 298},  # old intake basis
        {"date": "2026-09-16", "gongkao_new": 164,
         "count_basis": "public_sync_candidates"},
        {"date": "2026-09-17", "gongkao_new": 999,
         "count_basis": "public_sync_candidates"},
    ]}
    assert previous_public_volume(payload, date(2026, 9, 17))["gongkao_new"] == 164


def test_digest_webhooks_keeps_primary_and_deduplicates_extra_groups() -> None:
    assert digest_webhooks({
        "FEISHU_DIGEST_WEBHOOK": "https://open.feishu.test/old",
        "FEISHU_DIGEST_WEBHOOKS": (
            "https://open.feishu.test/new, https://open.feishu.test/old;"
            "https://open.feishu.test/third"
        ),
    }) == [
        "https://open.feishu.test/old",
        "https://open.feishu.test/new",
        "https://open.feishu.test/third",
    ]


def test_send_digest_posts_same_card_to_every_group(monkeypatch) -> None:
    response = Mock()
    response.json.return_value = {"code": 0}
    post = Mock(return_value=response)
    monkeypatch.setattr("app.notify_gongkao_digest.httpx.post", post)
    payload = {"msg_type": "interactive", "card": {"header": {}}}

    assert send_digest(payload, ["https://old.test", "https://new.test"]) == 2
    assert post.call_args_list == [
        call("https://old.test", json=payload, timeout=15, follow_redirects=True),
        call("https://new.test", json=payload, timeout=15, follow_redirects=True),
    ]
