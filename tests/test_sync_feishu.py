from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock, call, patch

import httpx

from app.sync_feishu import (
    CHINA_TZ,
    DEFAULT_GONGKAO_MAPPING,
    DEFAULT_QIUZHAO_MAPPING,
    FeishuClient,
    date_to_millis,
    diff_records,
    map_gongkao,
    map_qiuzhao,
    normalize,
    split_region,
    sync_table,
)


NOW = datetime(2026, 9, 5, 8, 30, tzinfo=CHINA_TZ)


def test_date_conversion_accepts_seconds_millis_iso_and_chinese_date() -> None:
    expected = int(datetime(2026, 9, 7, tzinfo=CHINA_TZ).timestamp() * 1000)
    assert date_to_millis(expected) == expected
    assert date_to_millis(expected // 1000) == expected
    assert date_to_millis("2026-09-07") == expected
    assert date_to_millis("2026年09月07日") == expected
    assert date_to_millis("") is None
    assert date_to_millis("not-a-date") is None


def test_region_uses_province_and_city_from_tags_or_location() -> None:
    assert split_region({"extra": {"province": "山东省", "tags": ["事业单位", "德州市"]}}) == "山东·德州"
    assert split_region({"extra": {"province": "河北", "location": "河北省石家庄市"}}) == "河北·石家庄"
    assert split_region({"extra": {"province": "重庆", "tags": ["重庆市", "事业单位", "2026"]}}) == "重庆"
    assert split_region({"extra": {"province": "河南", "tags": ["河南", "国央企正式", "郑州市"]}}) == "河南·郑州"
    assert split_region({"extra": {"province": "上海", "tags": ["上海", "上海市", "北京", "北京市"]}}) == "上海"
    assert split_region({"extra": {"province": "辽宁"}}) == "辽宁"


def test_gongkao_mapping_derives_dates_status_region_and_links() -> None:
    row = {
        "title": "德州市事业单位公开招聘公告",
        "url": "https://example.com/notice",
        "summary_zh": "招录人数：120 报名安排见公告",
        "extra": {
            "id": 987,
            "province": "山东",
            "tags": ["山东", "德州市", "事业单位"],
            "exam_type": "医疗卫生",
            "startSignUpTime": "2026-09-01",
            "endSignUpTime": "2026-09-07",
            "startWriteTime": "2026-09-20",
            "apply_url": "https://example.com/apply",
            "fresh_graduate": "是",
        },
    }
    fields = map_gongkao(row, now=NOW)
    assert fields["同步ID"] == "987"
    assert fields["地区"] == "山东·德州"
    assert fields["招录类型"] == "医疗"
    assert fields["招录人数"] == "120"
    assert fields["报名截止"] == date_to_millis("2026-09-07")
    assert fields["距截止天数"] == 2
    assert fields["报名状态"] == "报名中"
    assert "报名入口" not in fields
    assert fields["公告链接"] == {"text": "查看公告", "link": "https://example.com/notice"}
    assert fields["应届可报"] is True
    assert fields["来源"] == "自动"


def test_gongkao_mapping_clamps_expired_days_and_marks_waiting_for_exam() -> None:
    fields = map_gongkao({
        "title": "公告",
        "extra": {
            "id": "a1", "province": "天津", "exam_type": "未知类型",
            "endSignUpTime": "2026-09-01", "startWriteTime": "2026-09-10",
        },
    }, now=NOW)
    assert fields["距截止天数"] == 0
    assert fields["报名状态"] == "待笔试"
    assert fields["招录类型"] == "其他"


def test_qiuzhao_mapping_and_normalized_sync_id() -> None:
    row = {
        "company_name": " Acme（中国）有限公司 ",
        "company_type": "中央企业",
        "industry": "生物医药·AI",
        "title": " AI 研发工程师 ",
        "city": "上海",
        "deadline": "2026-09-12",
        "written_test": "有",
        "apply_url": "https://example.com/job",
        "announcement_url": "https://example.com/notice",
    }
    fields = map_qiuzhao(row, now=NOW)
    assert fields["同步ID"] == f"{normalize(row['company_name'])}|{normalize(row['title'])}"
    assert fields["企业性质"] == "央企"
    assert fields["网申截止"] == date_to_millis("2026-09-12")
    assert fields["距截止天数"] == 7
    assert fields["是否笔试"] is True
    assert fields["投递链接"]["link"] == "https://example.com/job"


def test_diff_creates_updates_deletes_and_completely_ignores_manual_rows() -> None:
    source = [
        {"同步ID": "same", "更新时间": 200, "公司名称": "A", "来源": "自动"},
        {"同步ID": "changed", "更新时间": 200, "公司名称": "B new", "来源": "自动"},
        {"同步ID": "new", "更新时间": 200, "公司名称": "C", "来源": "自动"},
        {"同步ID": "manual-id", "更新时间": 200, "公司名称": "from source", "来源": "自动"},
    ]
    existing = [
        {"record_id": "rec-same", "fields": {"同步ID": "same", "更新时间": 100, "公司名称": "A", "来源": "自动"}},
        {"record_id": "rec-change", "fields": {"同步ID": "changed", "更新时间": 100, "公司名称": "B old", "来源": "自动"}},
        {"record_id": "rec-stale", "fields": {"同步ID": "stale", "更新时间": 100, "公司名称": "D", "来源": "自动"}},
        {"record_id": "rec-manual", "fields": {"同步ID": "manual-id", "公司名称": "hand edited", "来源": "手动"}},
        {"record_id": "rec-manual-stale", "fields": {"同步ID": "manual-stale", "来源": "手动"}},
    ]
    creates, updates, deletes = diff_records(source, existing)
    assert [row["同步ID"] for row in creates] == ["new", "manual-id"]
    assert updates == [{"record_id": "rec-change", "fields": source[1]}]
    assert deletes == ["rec-stale"]


def test_diff_treats_feishu_rich_text_response_as_plain_source_text() -> None:
    source = [{"同步ID": "1", "更新时间": 200, "公司名称": "示例公司", "来源": "自动"}]
    existing = [{
        "record_id": "rec-1",
        "fields": {
            "同步ID": [{"type": "text", "text": "1"}],
            "更新时间": 100,
            "公司名称": [{"type": "text", "text": "示例公司"}],
            "来源": "自动",
        },
    }]
    assert diff_records(source, existing) == ([], [], [])


def test_sync_table_uses_fake_client_and_batches_diff_operations() -> None:
    client = Mock()
    client.list_records.return_value = [
        {"record_id": "old", "fields": {"同步ID": "old", "来源": "自动"}},
        {"record_id": "manual", "fields": {"同步ID": "keep", "来源": "手动"}},
    ]
    rows = [{"title": "岗位", "company": "公司", "url": "https://example.com"}]
    with patch("app.sync_feishu.time.sleep") as sleep:
        result = sync_table(
            client, "app-token", "table-id", rows, map_qiuzhao, DEFAULT_QIUZHAO_MAPPING,
            now=NOW,
        )
    client.list_records.assert_called_once_with("app-token", "table-id")
    client.batch_create.assert_called_once()
    created = client.batch_create.call_args.args[2][0]
    assert created["同步ID"] == "公司|岗位"
    assert client.batch_delete.call_args_list == [call("app-token", "table-id", ["old"])]
    client.batch_update.assert_not_called()
    sleep.assert_called_once_with(0.5)
    assert result == {"source": 1, "created": 1, "updated": 0, "deleted": 1, "skipped": 0}


def test_sync_table_caps_every_write_batch_at_500() -> None:
    client = Mock()
    client.list_records.return_value = []
    rows = [{"id": str(index)} for index in range(501)]

    def mapper(row, _mapping, *, now=None):
        return {"同步ID": row["id"], "更新时间": 1, "来源": "自动"}

    mapping = {"$sync_id": "同步ID", "$sync_time": "更新时间", "$source": "来源"}
    with patch("app.sync_feishu.time.sleep") as sleep:
        result = sync_table(client, "app", "table", rows, mapper, mapping, now=NOW)
    assert [len(item.args[2]) for item in client.batch_create.call_args_list] == [500, 1]
    sleep.assert_called_once_with(0.5)
    assert result["created"] == 501


def test_client_caches_token_and_refetches_it_once_after_401() -> None:
    calls = {"auth": 0, "records": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            calls["auth"] += 1
            return httpx.Response(200, json={
                "code": 0,
                "tenant_access_token": f"token-{calls['auth']}",
                "expire": 7200,
            })
        calls["records"] += 1
        if calls["records"] == 1:
            return httpx.Response(401, json={"code": 99991663, "msg": "token invalid"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    with FeishuClient("app-id", "secret", transport=httpx.MockTransport(handler)) as client:
        assert client.list_records("base", "table") == []
        assert client.list_records("base", "table") == []
    assert calls == {"auth": 2, "records": 3}


def test_default_mapping_has_every_required_feishu_field() -> None:
    assert set(DEFAULT_GONGKAO_MAPPING.values()) == {
        "同步ID", "更新时间", "地区", "招录类型", "招录单位·公告", "招录人数",
        "报名开始", "报名截止", "距截止天数", "笔试时间", "报名状态",
        "公告链接", "应届可报", "来源", "备注",
    }
    assert set(DEFAULT_QIUZHAO_MAPPING.values()) == {
        "同步ID", "更新时间", "公司名称", "企业性质", "行业", "招聘岗位", "工作地点", "学历要求",
        "届次", "网申截止", "距截止天数", "是否笔试", "投递链接", "公告链接", "来源", "备注",
    }
