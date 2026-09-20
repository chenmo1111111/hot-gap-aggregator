from __future__ import annotations

from datetime import datetime
from pathlib import Path
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
    sync_public_table,
    sync_instructions_if_enabled,
)


def test_public_sync_config_disables_legacy_instructions_table() -> None:
    import yaml

    config_path = Path(__file__).parents[1] / "config" / "feishu_public_sync.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["instructions_table_enabled"] is False
    assert "instructions_table_name" not in config


def test_disabled_instructions_sync_does_not_touch_feishu() -> None:
    client = Mock()

    assert sync_instructions_if_enabled(
        client, "base", {"instructions_table_enabled": False}
    ) is None
    client.assert_not_called()


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
        "岗位性质", "限户籍", "限专业", "应届", "应届要求", "服务期",
        "招录院校范围", "备注", "链接",
        "备用链接", "疑似重复", "可能重复于",
    }
    assert fields["类别"] == "事业单位"
    assert fields["招聘人数"] == "12"
    assert fields["链接"] == {"text": "查看公告", "link": "https://example.com/notice"}
    assert fields["备用链接"] is None
    assert fields["疑似重复"] is False
    assert fields["首次收录"] == int(datetime(2026, 9, 8, tzinfo=CHINA_TZ).timestamp() * 1000)
    assert "日期" not in fields
    assert "来源" not in fields
    assert fields["省份"] == "山东"
    assert fields["应届要求"] == "未明确"
    assert "同步ID" not in fields


def test_public_text_fields_use_slash_without_touching_typed_empty_fields() -> None:
    gongkao = map_public_gongkao({
        "title": "无人数公告", "url": "https://example.com/blank", "extra": {},
    })
    assert gongkao["招聘人数"] == "/"
    assert gongkao["最低学历"] == "/"
    assert gongkao["城市"] == "/"
    assert gongkao["招录院校范围"] == "/"
    assert gongkao["备注"] == "/"
    assert gongkao["报名开始"] is None
    assert gongkao["报名截止"] is None
    assert gongkao["报名状态"] is None

    qiuzhao = map_public_qiuzhao({
        "company_name": "公司", "position": "岗位", "announcement_url": "https://example.com/job",
    })
    assert qiuzhao["行业"] == "/"
    assert qiuzhao["工作地点"] == []
    assert qiuzhao["学历要求"] == "/"
    assert qiuzhao["投递链接"] is None


def test_public_gongkao_normalizes_placeholder_province_to_nationwide() -> None:
    fields = map_public_gongkao({
        "title": "军队文职公告",
        "url": "http://81rc.81.cn/sy/gzdt_210283/16484161.html",
        "extra": {"province": "详见正文", "exam_type": "军队文职"},
    })

    assert fields["省份"] == "全国"


def test_public_gongkao_does_not_expose_purchase_source() -> None:
    fields = map_public_gongkao({
        "title": "事业单位招聘公告",
        "url": "https://example.com/notice",
        "extra": {"id": "sheet:1", "upstream_source": "feishu_sheet"},
    })

    assert "来源" not in fields


def test_public_gongkao_prefers_original_source_over_internal_fenbi_api() -> None:
    fields = map_public_gongkao({
        "title": "事业单位招聘公告",
        "url": (
            "https://hera-webapp.fenbi.com/api/website/article/detail?"
            "deviceType=3&id=469067690430464&app=web"
        ),
        "extra": {
            "id": "469067690430464",
            "source_site": "fenbi",
            "source_url": "https://t.fenbi.com/s/00EXAMPLE",
        },
    })

    assert fields["链接"] == {
        "text": "查看公告",
        "link": "https://t.fenbi.com/s/00EXAMPLE",
    }


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


def test_public_schema_refuses_source_deletion_with_manual_or_unidentified_rows() -> None:
    current = [
        {"field_id": f"fld-{index}", "field_name": definition["field_name"],
         "type": definition["type"], "is_primary": index == 0}
        for index, definition in enumerate(GONGKAO_SCHEMA)
    ] + [{"field_id": "fld-source", "field_name": "来源", "type": 3}]
    for old_fields in (
        {"公告标题": "人工记录", "来源": "手动", "同步ID": "manual:1"},
        {"公告标题": "无ID记录", "来源": "自动"},
    ):
        client = _SchemaClient(records=[{"record_id": "rec-keep", "fields": old_fields}])
        client.list_fields = lambda _app, _table: current
        with pytest.raises(FeishuAPIError, match="拒绝删除来源列"):
            ensure_public_schema(
                client, "app", "table", GONGKAO_SCHEMA,
                deprecated_fields=GONGKAO_DEPRECATED_FIELDS,
            )
        assert client.deleted_fields == []


def test_public_schema_removes_source_only_after_all_rows_have_managed_ids() -> None:
    client = _SchemaClient(records=[{
        "record_id": "rec-auto",
        "fields": {"公告标题": "自动记录", "来源": "自动·政府网站", "同步ID": "gov:1"},
    }])
    client.list_fields = lambda _app, _table: [
        {"field_id": f"fld-{index}", "field_name": definition["field_name"],
         "type": definition["type"], "is_primary": index == 0}
        for index, definition in enumerate(GONGKAO_SCHEMA)
    ] + [{"field_id": "fld-source", "field_name": "来源", "type": 3}]
    assert ensure_public_schema(
        client, "app", "table", GONGKAO_SCHEMA,
        deprecated_fields=GONGKAO_DEPRECATED_FIELDS,
    ) is True
    assert client.deleted_fields == ["fld-source"]


def test_public_schema_requires_private_registry_before_deleting_sync_id() -> None:
    client = _SchemaClient(records=[{
        "record_id": "rec-auto", "fields": {"公告标题": "自动记录", "同步ID": "gov:1"},
    }])
    client.list_fields = lambda _app, _table: [
        {"field_id": f"fld-{index}", "field_name": definition["field_name"],
         "type": definition["type"], "is_primary": index == 0}
        for index, definition in enumerate(GONGKAO_SCHEMA)
    ] + [{"field_id": "fld-sync", "field_name": "同步ID", "type": 1}]
    with pytest.raises(FeishuAPIError, match="缺少私有同步台账"):
        ensure_public_schema(
            client, "app", "table", GONGKAO_SCHEMA,
            deprecated_fields=GONGKAO_DEPRECATED_FIELDS,
        )
    with pytest.raises(FeishuAPIError, match="台账不一致"):
        ensure_public_schema(
            client, "app", "table", GONGKAO_SCHEMA,
            deprecated_fields=GONGKAO_DEPRECATED_FIELDS,
            managed_record_ids=set(),
        )
    assert client.deleted_fields == []
    assert ensure_public_schema(
        client, "app", "table", GONGKAO_SCHEMA,
        deprecated_fields=GONGKAO_DEPRECATED_FIELDS,
        managed_record_ids={"rec-auto"},
    ) is True
    assert client.deleted_fields == ["fld-sync"]


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


def test_public_diff_uses_sync_id_to_protect_manual_rows_without_source_column() -> None:
    existing = [
        {"record_id": "rec-manual", "fields": {
            "公告标题": "人工标题", "链接": {"link": "https://same.test"},
        }},
        {"record_id": "rec-auto-duplicate", "fields": {
            "公告标题": "旧自动标题", "链接": {"link": "https://same.test"},
            "同步ID": "gov:1",
        }},
        {"record_id": "rec-auto-stale", "fields": {
            "公告标题": "过期自动标题", "链接": {"link": "https://old.test"},
            "同步ID": "gov:2",
        }},
    ]
    source = [{
        "公告标题": "新自动标题", "链接": {"link": "https://same.test"},
        "同步ID": "gov:1",
    }]
    creates, updates, deletes = diff_public_records(
        source, existing, gongkao_key, managed_id_field="同步ID",
    )
    assert creates == []
    assert updates == []
    assert set(deletes) == {"rec-auto-duplicate", "rec-auto-stale"}


def test_public_diff_keeps_new_manual_row_without_link_or_sync_id() -> None:
    manual = [{"record_id": "rec-manual", "fields": {"公告标题": "用户自行记录"}}]
    assert diff_public_records(
        [], manual, gongkao_key, managed_id_field="同步ID",
    ) == ([], [], [])


def test_private_registry_keeps_manual_rows_and_deletes_only_managed_rows() -> None:
    existing = [
        {"record_id": "rec-manual", "fields": {
            "公告标题": "人工标题", "链接": {"link": "https://same.test"},
        }},
        {"record_id": "rec-auto-duplicate", "fields": {
            "公告标题": "旧自动标题", "链接": {"link": "https://same.test"},
        }},
        {"record_id": "rec-auto-stale", "fields": {
            "公告标题": "过期自动标题", "链接": {"link": "https://old.test"},
        }},
    ]
    source = [{"公告标题": "新自动标题", "链接": {"link": "https://same.test"}}]
    creates, updates, deletes = diff_public_records(
        source, existing, gongkao_key,
        managed_record_ids={"rec-auto-duplicate", "rec-auto-stale"},
    )
    assert creates == []
    assert updates == []
    assert set(deletes) == {"rec-auto-duplicate", "rec-auto-stale"}


def test_public_sync_persists_new_managed_record_ids() -> None:
    client = Mock()
    client.list_records.return_value = []
    client.batch_create.return_value = {
        "data": {"records": [{"record_id": "rec-created"}]},
    }
    ids: set[str] = set()
    saved: list[set[str]] = []
    result = sync_public_table(
        client, "app", "table",
        [{"公告标题": "新公告", "链接": {"link": "https://new.test"}}],
        dict, gongkao_key,
        managed_record_ids=ids,
        save_managed_record_ids=lambda: saved.append(set(ids)),
    )
    assert result["created"] == 1
    assert ids == {"rec-created"}
    assert saved == [{"rec-created"}]


def test_public_sync_rejects_create_response_without_record_id() -> None:
    client = Mock()
    client.list_records.return_value = []
    client.batch_create.return_value = {"data": {"records": [{}]}}
    ids: set[str] = set()
    with pytest.raises(FeishuAPIError, match="未返回完整 record_id"):
        sync_public_table(
            client, "app", "table",
            [{"公告标题": "新公告", "链接": {"link": "https://new.test"}}],
            dict, gongkao_key,
            managed_record_ids=ids,
            save_managed_record_ids=lambda: None,
        )
    assert ids == set()
    client.batch_delete.assert_not_called()


def test_public_sync_removes_deleted_ids_from_private_registry() -> None:
    client = Mock()
    client.list_records.return_value = [{
        "record_id": "rec-old",
        "fields": {"公告标题": "旧公告", "链接": {"link": "https://old.test"}},
    }]
    ids = {"rec-old"}
    saved: list[set[str]] = []
    result = sync_public_table(
        client, "app", "table", [], dict, gongkao_key,
        managed_record_ids=ids,
        save_managed_record_ids=lambda: saved.append(set(ids)),
    )
    assert result["deleted"] == 1
    assert ids == set()
    assert saved == [set()]


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


def test_public_diff_deletes_provenance_labeled_automatic_row() -> None:
    existing = [{
        "record_id": "rec-stale-fenbi",
        "fields": {
            "公告标题": "黑龙江省近期部分招聘信息汇总",
            "链接": {
                "link": (
                    "https://hera-webapp.fenbi.com/api/website/article/detail?"
                    "deviceType=3&id=469068479483904&app=web"
                ),
            },
            "来源": "自动·网站-粉笔",
        },
    }]

    creates, updates, deletes = diff_public_records(
        [], existing, gongkao_key, source_field="来源",
    )

    assert creates == []
    assert updates == []
    assert deletes == ["rec-stale-fenbi"]


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
