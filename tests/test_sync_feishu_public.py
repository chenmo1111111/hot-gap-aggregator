from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

import pytest

from app.sync_feishu import CHINA_TZ, FeishuAPIError
from app.sync_feishu_public import (
    GONGKAO_DEPRECATED_FIELDS,
    GONGKAO_SCHEMA,
    diff_public_records,
    ensure_instructions_table,
    ensure_public_schema,
    gongkao_key,
    map_public_gongkao,
    map_public_qiuzhao,
    qiuzhao_key,
    slash_public_updates,
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
        "公告标题", "首次收录", "类别", "招聘人数", "最低学历",
        "报名开始", "报名截止", "报名状态", "省份", "城市", "单位名称",
        "岗位性质", "限户籍", "限专业", "应届", "服务期",
        "招录院校范围", "备注", "链接", "同步ID", "来源",
    }
    assert fields["类别"] == "事业单位"
    assert fields["招聘人数"] == "12"
    assert fields["链接"] == {"text": "查看公告", "link": "https://example.com/notice"}
    assert fields["首次收录"] == int(datetime(2026, 9, 8, tzinfo=CHINA_TZ).timestamp() * 1000)
    assert "日期" not in fields
    assert fields["来源"] == "自动"
    assert fields["省份"] == "山东"
    assert fields["同步ID"] == "url:2888a51e1ec64bad2cafe9ce"


def test_public_text_fields_use_slash_without_touching_typed_empty_fields() -> None:
    gongkao = map_public_gongkao({
        "title": "无人数公告", "url": "https://example.com/blank", "extra": {},
    })
    assert gongkao["招聘人数"] == "/"
    assert gongkao["最低学历"] == "/"
    assert gongkao["招录院校范围"] == "/"
    assert gongkao["备注"] == "/"
    assert gongkao["报名开始"] is None
    assert gongkao["报名截止"] is None
    assert gongkao["报名状态"] is None

    qiuzhao = map_public_qiuzhao({
        "company_name": "公司", "position": "岗位", "announcement_url": "https://example.com/job",
    })
    assert qiuzhao["行业"] == "/"
    assert qiuzhao["工作地点"] == "/"
    assert qiuzhao["学历要求"] == "/"
    assert qiuzhao["投递链接"] is None


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


def test_public_qiuzhao_rejects_fenbi_calendar_as_application_url() -> None:
    fields = map_public_qiuzhao({
        "company_name": "招商证券",
        "position": "2027届校园招聘",
        "apply_url": "https://www.fenbi.com/page/kaoshidetail/123",
        "announcement_url": "https://www.fenbi.com/page/kaoshidetail/123",
    })
    assert fields["投递链接"] is None
    assert fields["公告链接"]["link"].startswith("https://www.fenbi.com/")


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


def test_public_diff_treats_slash_as_empty_and_placeholder_backfill_runs_once() -> None:
    source = [{
        "公告标题": "公告", "链接": {"link": "https://a.test/1"}, "备注": "/",
    }]
    blank = [{
        "record_id": "rec-1",
        "fields": {"公告标题": "公告", "链接": {"link": "https://a.test/1"}, "备注": ""},
    }]
    filled = [{
        "record_id": "rec-1",
        "fields": {"公告标题": "公告", "链接": {"link": "https://a.test/1"}, "备注": "/"},
    }]
    assert diff_public_records(source, blank, gongkao_key) == ([], [], [])
    assert slash_public_updates(source, blank, gongkao_key) == [
        {"record_id": "rec-1", "fields": source[0]}
    ]
    assert slash_public_updates(source, filled, gongkao_key) == []


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
        {"field_id": "fld-date", "field_name": "日期", "type": 5},
    ])
    client.list_fields = lambda _app, _table: current

    changed = ensure_public_schema(
        client, "app", "table", GONGKAO_SCHEMA,
        deprecated_fields=GONGKAO_DEPRECATED_FIELDS,
    )

    assert changed is True
    assert client.created == []
    assert client.deleted_fields == ["fld-bishi", "fld-school", "fld-date"]


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


def test_force_delete_removes_routed_expired_row() -> None:
    existing = [{
        "record_id": "rec-campus",
        "fields": {"链接": {"link": "https://old.test/campus"}, "报名状态": "已截止"},
    }]
    creates, updates, deletes = diff_public_records(
        [], existing, gongkao_key,
        preserve_missing=lambda fields: fields.get("报名状态") == "已截止",
        force_delete_keys={"url:https://old.test/campus"},
    )
    assert (creates, updates, deletes) == ([], [], ["rec-campus"])


def test_public_diff_never_updates_or_deletes_manual_rows() -> None:
    source = [{
        "公告标题": "自动标题", "链接": {"link": "https://same.test"}, "来源": "自动",
    }]
    manual = [{
        "record_id": "rec-manual",
        "fields": {"公告标题": "人工标题", "链接": {"link": "https://same.test"}, "来源": "手动"},
    }]
    assert diff_public_records(
        source, manual, gongkao_key, source_field="来源",
        force_delete_keys={"url:https://same.test"},
    ) == ([], [], [])


def test_public_diff_deletes_known_legacy_auto_but_preserves_unknown_blank_row() -> None:
    existing = [
        {"record_id": "rec-known", "fields": {"链接": {"link": "https://known.test"}}},
        {"record_id": "rec-unknown", "fields": {"链接": {"link": "https://unknown.test"}}},
    ]
    creates, updates, deletes = diff_public_records(
        [], existing, gongkao_key, source_field="来源",
        known_auto_keys={"url:https://known.test"},
    )
    assert (creates, updates, deletes) == ([], [], ["rec-known"])


def test_replaced_gongkao_links_are_collected_for_forced_deletion() -> None:
    from app.sync_feishu_public import _replaced_gongkao_link_keys

    rows = [{
        "url": "https://official.test/notice",
        "extra": {
            "replaced_urls": [
                "https://www.fenbi.com/page/exam-timeline-detail/908859",
                "https://hera-webapp.fenbi.com/api/website/article/detail?id=468944",
            ]
        },
    }]

    assert _replaced_gongkao_link_keys(rows) == {
        "url:https://www.fenbi.com/page/exam-timeline-detail/908859",
        "url:https://hera-webapp.fenbi.com/api/website/article/detail?id=468944",
    }


def test_instructions_table_is_created_and_seeded() -> None:
    client = Mock()
    client.list_tables.return_value = []
    client.create_table.return_value = {"table_id": "tbl-guide", "name": "使用说明"}
    client.list_fields.return_value = [
        {"field_id": "fld-primary", "field_name": "多行文本", "type": 1, "is_primary": True},
    ]
    client.list_records.side_effect = [[], []]

    table_id, result = ensure_instructions_table(client, "base")

    assert table_id == "tbl-guide"
    client.create_table.assert_called_once_with("base", "使用说明")
    assert client.batch_create.call_count == 1
    created_rows = client.batch_create.call_args.args[2]
    assert len(created_rows) == 10
    assert created_rows[0]["视图"] == "总说明"
    assert result["created"] == 10
    assert result["table_created"] == 1
