from __future__ import annotations

from datetime import date

from app.notify_gongkao_digest import build_card, select_top10


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
