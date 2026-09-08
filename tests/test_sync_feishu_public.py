from __future__ import annotations

from datetime import datetime

import pytest

from app.sync_feishu import CHINA_TZ, FeishuAPIError
from app.sync_feishu_public import (
    GONGKAO_SCHEMA,
    diff_public_records,
    ensure_public_schema,
    gongkao_key,
    map_public_gongkao,
    map_public_qiuzhao,
    qiuzhao_key,
)


def test_map_public_gongkao_uses_only_display_fields() -> None:
    fields = map_public_gongkao({
        "title": "示例招聘公告",
        "url": "https://example.com/notice",
        "published_at": "2026-09-04",
        "extra": {
            "exam_type": "事业编",
            "province": "山东省",
            "recruit_count": "12",
            "endSignUpTime": "2026-09-11",
            "first_seen": "2026-09-08",
            "notes": "联考",
        },
    })

    assert set(fields) == {
        "公告标题", "日期", "首次收录", "类别", "招聘人数", "截止日期", "省份", "链接",
        "报名状态", "距截止天数", "细分类别", "限户籍", "限专业", "学历要求",
        "限应届", "服务期", "招录院校范围", "备注",
    }
    assert fields["类别"] == "事业单位"
    assert fields["招聘人数"] == "12"
    assert fields["链接"] == {"text": "查看公告", "link": "https://example.com/notice"}
    assert fields["日期"] == int(datetime(2026, 9, 4, tzinfo=CHINA_TZ).timestamp() * 1000)
    assert fields["首次收录"] == int(datetime(2026, 9, 8, tzinfo=CHINA_TZ).timestamp() * 1000)
    assert list(fields)[-1] == "备注"


def test_map_public_selection_exposes_generic_school_scope_only() -> None:
    fields = map_public_gongkao({
        "title": "辽宁定向选调公告",
        "url": "https://example.com/xuandiao",
        "extra": {
            "exam_type": "选调生", "detail_category": "选调生",
            "enrichment_status": "ok", "xuandiao_school_scope": "指定40所高校（名单见公告）",
            "my_school_eligible": True,
        },
    })

    assert fields["招录院校范围"] == "指定40所高校（名单见公告）"
    assert "本校可报" not in fields
    assert "笔试科目" not in fields


def test_map_public_qiuzhao_uses_visible_natural_key() -> None:
    fields = map_public_qiuzhao({
        "company_name": "示例公司",
        "company_type": "中央企业",
        "position": "研发工程师",
        "updated_at": 1_788_480_000_000,
        "written_test": True,
        "apply_url": "https://example.com/apply",
    })

    assert fields["企业性质"] == "央企"
    assert fields["是否笔试"] is True
    assert fields["投递链接"]["link"] == "https://example.com/apply"
    assert qiuzhao_key(fields) == "job:示例公司|研发工程师"


def test_diff_public_records_creates_updates_deletes_and_deduplicates() -> None:
    source = [
        {"公告标题": "保留但更新", "链接": {"text": "查看公告", "link": "https://a.test/1"}},
        {"公告标题": "新增", "链接": {"text": "查看公告", "link": "https://b.test/2"}},
    ]
    existing = [
        {"record_id": "rec-update", "fields": {"公告标题": "旧标题", "链接": {"link": "https://a.test/1/"}}},
        {"record_id": "rec-delete", "fields": {"公告标题": "删除", "链接": {"link": "https://c.test/3"}}},
        {"record_id": "rec-blank", "fields": {}},
    ]

    creates, updates, deletes = diff_public_records(source, existing, gongkao_key)

    assert [row["公告标题"] for row in creates] == ["新增"]
    assert updates == [{"record_id": "rec-update", "fields": source[0]}]
    assert deletes == ["rec-blank", "rec-delete"]


class _SchemaClient:
    def __init__(self, *, records=None) -> None:
        self.records = records or []
        self.updated = []
        self.deleted_fields = []
        self.created = []
        self.deleted_records = []

    def list_fields(self, _app, _table):
        return [
            {"field_id": "fld-primary", "field_name": "多行文本", "type": 1, "is_primary": True},
            {"field_id": "fld-extra", "field_name": "附件", "type": 17, "is_primary": False},
        ]

    def list_records(self, _app, _table):
        return self.records

    def update_field(self, _app, _table, field_id, definition):
        self.updated.append((field_id, definition))

    def delete_field(self, _app, _table, field_id):
        self.deleted_fields.append(field_id)

    def create_field(self, _app, _table, definition):
        self.created.append(definition)

    def batch_delete(self, _app, _table, record_ids):
        self.deleted_records.extend(record_ids)


def test_ensure_public_schema_initializes_only_blank_table() -> None:
    client = _SchemaClient(records=[{"record_id": "rec-empty", "fields": {}}])

    initialized = ensure_public_schema(client, "app", "table", GONGKAO_SCHEMA)

    assert initialized is True
    assert client.updated[0][0] == "fld-primary"
    assert client.updated[0][1]["field_name"] == "公告标题"
    assert client.deleted_fields == ["fld-extra"]
    assert [field["field_name"] for field in client.created] == [
        field["field_name"] for field in GONGKAO_SCHEMA[1:]
    ]
    assert client.deleted_records == ["rec-empty"]


def test_ensure_public_schema_refuses_nonempty_table() -> None:
    client = _SchemaClient(records=[{"record_id": "rec-user", "fields": {"多行文本": "用户数据"}}])
    with pytest.raises(FeishuAPIError, match="拒绝自动修改"):
        ensure_public_schema(client, "app", "table", GONGKAO_SCHEMA)


def test_ensure_public_schema_only_adds_missing_fields_to_live_table() -> None:
    client = _SchemaClient(records=[{"record_id": "rec-live", "fields": {"公告标题": "保留"}}])
    client.list_fields = lambda _app, _table: [
        {"field_id": f"fld-{index}", "field_name": definition["field_name"],
         "type": definition["type"], "is_primary": index == 0}
        for index, definition in enumerate(GONGKAO_SCHEMA[:8])
    ]

    changed = ensure_public_schema(client, "app", "table", GONGKAO_SCHEMA)

    assert changed is True
    assert client.updated == []
    assert client.deleted_fields == []
    assert client.deleted_records == []
    assert [field["field_name"] for field in client.created] == [
        field["field_name"] for field in GONGKAO_SCHEMA[8:]
    ]


def test_ensure_public_schema_deletes_only_declared_deprecated_fields() -> None:
    client = _SchemaClient(records=[{"record_id": "rec-live", "fields": {"公告标题": "保留"}}])
    current = [
        {"field_id": f"fld-{index}", "field_name": definition["field_name"],
         "type": definition["type"], "is_primary": index == 0}
        for index, definition in enumerate(GONGKAO_SCHEMA)
    ]
    current.extend([
        {"field_id": "fld-bishi", "field_name": "笔试科目", "type": 1},
        {"field_id": "fld-school", "field_name": "本校可报", "type": 7},
    ])
    client.list_fields = lambda _app, _table: current

    changed = ensure_public_schema(
        client, "app", "table", GONGKAO_SCHEMA,
        deprecated_fields=("笔试科目", "本校可报"),
    )

    assert changed is True
    assert client.created == []
    assert client.deleted_fields == ["fld-bishi", "fld-school"]


def test_diff_preserves_expired_missing_gongkao_row() -> None:
    existing = [{
        "record_id": "rec-expired",
        "fields": {"链接": {"link": "https://old.test"}, "报名状态": "已截止"},
    }]
    creates, updates, deletes = diff_public_records(
        [], existing, gongkao_key,
        preserve_missing=lambda fields: fields.get("报名状态") == "已截止",
    )
    assert (creates, updates, deletes) == ([], [], [])
