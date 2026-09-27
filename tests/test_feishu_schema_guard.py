from __future__ import annotations

import pytest

from app.feishu_schema_guard import (
    SchemaDriftError, canonical_schema, sanitize_fields, schema_diff, validate_live_schema,
)


BASELINE = {
    "fields": [
        {"name": "公司名称", "type": 1, "primary": True},
        {"name": "工作地点", "type": 4, "primary": False, "options": ["北京", "上海"]},
    ],
    "views": [{"name": "全部", "type": "grid"}],
}


def test_schema_diff_reports_field_type_option_and_view_changes() -> None:
    actual = {
        "fields": [
            {"name": "公司名称", "type": 1, "primary": True},
            {"name": "工作地点", "type": 1, "primary": False},
            {"name": "旧字段", "type": 1, "primary": False},
        ],
        "views": [{"name": "另一个", "type": "grid"}],
    }
    text = "；".join(schema_diff(BASELINE, actual))
    assert "新增字段：旧字段" in text
    assert "工作地点类型" in text
    assert "缺少基准视图：全部" in text


def test_schema_diff_allows_additional_user_views() -> None:
    actual = {
        **BASELINE,
        "views": [
            {"name": "全部", "type": "grid"},
            {"name": "江苏", "type": "grid"},
            {"name": "无锡", "type": "grid"},
            {"name": "苏州", "type": "grid"},
            {"name": "南京", "type": "grid"},
            {"name": "常州", "type": "grid"},
        ],
    }

    assert schema_diff(BASELINE, actual) == []


def test_schema_diff_still_blocks_missing_or_retyped_baseline_views() -> None:
    missing = {**BASELINE, "views": []}
    retyped = {**BASELINE, "views": [{"name": "全部", "type": "kanban"}]}

    assert schema_diff(BASELINE, missing) == ["缺少基准视图：全部"]
    assert schema_diff(BASELINE, retyped) == ["视图全部类型 kanban != grid"]


def test_validate_live_schema_stops_before_write() -> None:
    class Client:
        def list_fields(self, *_args):
            return [{"field_name": "公司名称", "type": 1, "is_primary": True}]

        def list_views(self, *_args):
            return [{"view_name": "全部", "view_type": "grid"}]

    with pytest.raises(SchemaDriftError, match="缺少字段"):
        validate_live_schema(Client(), "app", "table", BASELINE)


def test_sanitize_fields_never_creates_unknown_options_or_deleted_fields() -> None:
    assert sanitize_fields(
        {"公司名称": "甲", "工作地点": ["北京", "深圳"], "已删除": "x"}, BASELINE,
    ) == {"公司名称": "甲", "工作地点": ["北京"]}


def test_sanitize_fields_falls_unknown_automatic_source_back_to_managed_marker() -> None:
    expected = {
        "fields": [{"name": "来源", "type": 3, "options": ["自动", "手动"]}],
        "views": [],
    }
    assert sanitize_fields({"来源": "自动·编程导航"}, expected) == {"来源": "自动"}
    assert sanitize_fields({"来源": "人工导入"}, expected) == {"来源": None}


def test_canonical_schema_keeps_complete_option_order() -> None:
    result = canonical_schema(
        [{
            "field_name": "工作地点", "type": 4, "is_primary": False,
            "property": {"options": [{"id": "1", "name": "北京"}, {"id": "2", "name": "上海"}]},
        }],
        [{"view_name": "全部", "view_type": "grid"}],
    )
    assert result["fields"][0]["options"] == ["北京", "上海"]


def test_schema_diff_ignores_unstable_read_only_primary_flag() -> None:
    actual = {
        **BASELINE,
        "fields": [
            {**BASELINE["fields"][0], "primary": False},
            BASELINE["fields"][1],
        ],
    }
    assert schema_diff(BASELINE, actual) == []
