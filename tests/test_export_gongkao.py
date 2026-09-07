from __future__ import annotations

import json

from app.export_gongkao import merge_gongkao_payloads, write_gongkao


def _item(identifier: str, title: str, url: str, province: str = "山东") -> dict:
    return {
        "title": title,
        "url": url,
        "extra": {"id": identifier, "province": province},
    }


def test_merge_keeps_base_first_and_skips_sheet_duplicates() -> None:
    base = {"generated_at": "old", "items": [_item("base-1", "同一公告", "https://a.test/1")]}
    sheet = {
        "items": [
            _item("sheet-duplicate-url", "不同标题", "http://a.test/1/"),
            _item("sheet-duplicate-title", "同一公告", "https://b.test/2"),
            _item("sheet-new", "新增公告", "https://c.test/3"),
        ]
    }

    output = merge_gongkao_payloads(base, sheet)

    assert [item["extra"]["id"] for item in output["items"]] == ["base-1", "sheet-new"]
    assert output["status"]["base_item_count"] == 1
    assert output["status"]["sheet_added_count"] == 1
    assert output["status"]["sheet_duplicate_count"] == 2
    assert [item["rank"] for item in output["items"]] == [1, 2]


def test_write_gongkao_uses_snapshot_when_present_and_writes_separate_output(tmp_path) -> None:
    base = {"items": [_item("base", "原公告", "https://a.test/1")]}
    sheet = {"items": [_item("sheet", "补充公告", "https://b.test/2")]}
    (tmp_path / "gongkao.json").write_text(
        json.dumps(base, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "gongkao_sheet.json").write_text(
        json.dumps(sheet, ensure_ascii=False), encoding="utf-8"
    )

    result = write_gongkao(tmp_path)

    written = json.loads((tmp_path / "gongkao_feishu.json").read_text(encoding="utf-8"))
    assert written == result
    assert len(result["items"]) == 2
    assert json.loads((tmp_path / "gongkao.json").read_text(encoding="utf-8")) == base
