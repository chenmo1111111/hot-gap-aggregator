from __future__ import annotations

import json
from pathlib import Path

from app.capture_xiaozhaoya import build_snapshot, load_config
from app.collect_xiaozhaoya import run
from app.collectors.xiaozhaoya import HOME_URL, resolve_public_url, split_records


FIXTURE = Path(__file__).parent / "fixtures" / "xiaozhaoya_recruitment_list.json"


def test_load_config_reads_private_base_url_from_environment(
    tmp_path: Path, monkeypatch,
) -> None:
    config_path = tmp_path / "xiaozhaoya.yaml"
    config_path.write_text(
        "api_origin: https://internal-api-space.feishu.cn\n"
        "initial_table_id: table-from-config\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "XIAOZHAOYA_FEISHU_BASE_URL",
        "https://tenant.feishu.cn/base/private-token?table=table-from-url&view=view-from-url",
    )

    config = load_config(config_path)

    assert config["page_url"].startswith("https://tenant.feishu.cn/base/")
    assert config["app_token"] == "private-token"
    assert config["initial_table_id"] == "table-from-config"


def test_split_records_routes_campus_job_to_qiuzhao_with_source_label() -> None:
    qiuzhao, gongkao = split_records([{
        "recruitmentId": 44604,
        "updateDate": "2026-09-09",
        "companyName": "飞腾",
        "fullName": "飞腾信息技术有限公司",
        "companyTypeName": "央国企",
        "industryName": "通信/电子/半导体",
        "batchNameList": "秋招专场",
        "jobTitle": "芯片研发岗",
        "announcementTitle": "飞腾2027届校园招聘",
        "educationLevelNameList": "本科, 硕士",
        "graduationYearList": "2027",
        "cityNameList": "北京市, 天津市",
        "applicationDeadline": "2026-10-10",
        "applicationMethod": "https://www.phytium.com.cn/recruitment/campus_recruit",
        "hasWrittenTest": "有笔试",
    }])

    assert gongkao == []
    assert qiuzhao[0]["company_name"] == "飞腾"
    assert qiuzhao[0]["company_type"] == "央企"
    assert qiuzhao[0]["source_label"] == "校招鸭"
    assert qiuzhao[0]["source_record_id"] == "xiaozhaoya:44604"
    assert qiuzhao[0]["apply_url"].startswith("https://www.phytium.com.cn/")


def test_split_records_routes_public_notice_to_gongkao() -> None:
    qiuzhao, gongkao = split_records([{
        "recruitmentId": 90001,
        "updateDate": "2026-09-10",
        "companyName": "某市人社局",
        "announcementTitle": "某市2026年事业单位公开招聘工作人员公告",
        "jobTitle": "管理岗位",
        "announcementLink": "https://rsj.example.gov.cn/notice/90001.html",
        "applicationDeadline": "2026-09-20",
        "recruitmentCount": "53人",
    }])

    assert qiuzhao == []
    assert gongkao[0]["extra"]["exam_type"] == "事业单位"
    assert gongkao[0]["extra"]["government_source"] is True
    assert gongkao[0]["extra"]["source_label"] == "校招鸭"


def test_split_records_routes_government_agency_base_row_to_gongkao() -> None:
    qiuzhao, gongkao = split_records([{
        "recruitmentId": 90002,
        "updateDate": "2026-09-10",
        "companyName": "某市机关",
        "companyTypeName": "政府机关",
        "announcementTitle": "某市机关招聘",
        "jobTitle": "综合管理岗",
        "announcementLink": "https://example.com/notice/90002",
    }])

    assert qiuzhao == []
    assert gongkao[0]["extra"]["recruitment_id"] == "90002"


def test_resolve_public_url_ignores_bare_site_root() -> None:
    assert resolve_public_url("/") == ""
    assert resolve_public_url("官网：https://jobs.example.com/apply") == "https://jobs.example.com/apply"


def test_base_snapshot_maps_real_feishu_field_shape_and_detail_id() -> None:
    metadata = {
        "fieldMap": {
            "fld7GAjFoX": {"name": "公司名称", "type": 1},
            "fld5YIB14J": {"name": "招聘岗位", "type": 1},
            "fld6pVXeiP": {"name": "公告链接", "type": 15},
            "fld3c8OxWn": {"name": "投递方式", "type": 15},
            "fld7zxhBG1": {"name": "更新时间", "type": 1},
            "fld70CcIc6": {"name": "截止时间", "type": 1},
            "fld4xgSlJ4": {
                "name": "企业性质", "type": 3,
                "property": {"options": [{"id": "opt49Ybz3L", "name": "外企"}]},
            },
            "fldpH5TJKO": {
                "name": "学历要求", "type": 4,
                "property": {"options": [
                    {"id": "optzUuZP9L", "name": "本科"},
                    {"id": "optZljmSDv", "name": "硕士"},
                ]},
            },
            "fldcQhaDut": {
                "name": "批次", "type": 4,
                "property": {"options": [
                    {"id": "optQfy5bwB", "name": "实习"},
                    {"id": "optIEDRBke", "name": "秋招专场"},
                ]},
            },
        },
        "viewMap": {
            "vew4vJvx97": {"name": "🔥网申总表", "type": 1},
            "vewmceLy67": {"name": "实习信息", "type": 1},
        },
    }
    chunks = [{"recordMap": {"rec27K2TNDnxAy": {
        "fld7GAjFoX": {"value": [{"type": "text", "text": "高盛"}]},
        "fld5YIB14J": {"value": [{"type": "text", "text": "暑期分析员;全职分析员"}]},
        "fld6pVXeiP": {"value": [{
            "type": "url", "text": "校招鸭详情",
            "link": "https://www.xiaozhaoya.com/detail?id=41524",
        }]},
        "fld3c8OxWn": {"value": [{
            "type": "url", "text": "官网投递", "link": "https://example.com/apply",
        }]},
        "fld7zxhBG1": {"value": [{"type": "text", "text": "2026-07-06"}]},
        "fld70CcIc6": {"value": [{"type": "text", "text": "2026-10-05"}]},
        "fld4xgSlJ4": {"value": "opt49Ybz3L"},
        "fldpH5TJKO": {"value": ["optzUuZP9L", "optZljmSDv"]},
        "fldcQhaDut": {"value": ["optQfy5bwB", "optIEDRBke"]},
    }}}]

    snapshot = build_snapshot(
        [("tblkKIbEMShri8pB", "🔥 26、27 校招汇总表", metadata, chunks)],
        minimum_items=1,
        generated_at="2026-09-11T09:00:00+08:00",
    )

    assert snapshot["source"] == "xiaozhaoya_feishu_base"
    assert snapshot["status"]["scheduled"] is False
    assert snapshot["status"]["source_views"]["🔥 26、27 校招汇总表"] == [
        "🔥网申总表", "实习信息",
    ]
    row = snapshot["items"][0]
    assert row["recruitmentId"] == "41524"
    assert row["companyName"] == "高盛"
    assert row["companyTypeName"] == "外企"
    assert row["educationLevelNameList"] == "本科、硕士"
    assert row["batchNameList"] == "实习、秋招专场"
    assert row["announcementTitle"] == "高盛招聘"
    assert row["applicationMethod"] == "https://example.com/apply"


def test_real_page_fixture_classification_link_priority_and_noise_filter() -> None:
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(records) == 12
    assert {
        "recruitmentId", "updateDate", "companyName", "companyTypeName", "fullName",
        "industryName", "jobCategoryNameList", "jobTitle", "educationLevelNameList",
        "graduationYearList", "majorRequirements", "cityNameList", "announcementDate",
        "applicationDeadline", "sourceName", "announcementTitle", "announcementLink",
        "applicationMethod", "hasWrittenTest", "companyProfile", "recruitmentCount",
        "workAddress", "salary", "jobContent",
    }.issubset(records[0])

    qiuzhao, gongkao = split_records(records)

    assert len(qiuzhao) == 7
    assert len(gongkao) == 4
    first = next(row for row in qiuzhao if row["source_record_id"] == "xiaozhaoya:50001")
    assert first["apply_url"] == "https://example.com/apply/50001"
    assert first["announcement_url"] == "https://example.com/notice/50001"
    assert first["position"] == "芯片研发岗、软件工程师"
    assert first["location"] == "北京、天津"
    assert first["major"] == "计算机类、电子信息类"
    assert first["extra"]["source_site"] == "xiaozhaoya"
    missing = next(row for row in qiuzhao if row["source_record_id"] == "xiaozhaoya:50008")
    assert missing["apply_url"] == HOME_URL
    assert "未提供有效" in missing["notes"]
    assert any(row["extra"]["id"] == "xiaozhaoya:50005" for row in gongkao)
    assert all("宣讲会" not in row.get("title", "") for row in gongkao)
    assert all("宣讲会" not in row.get("position", "") for row in qiuzhao)


def test_s1_splitter_reads_raw_snapshot_and_writes_both_feeds(tmp_path: Path) -> None:
    input_path = tmp_path / "xiaozhaoya.json"
    input_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-09-10T07:30:00+08:00",
                "total": 12,
                "items": json.loads(FIXTURE.read_text(encoding="utf-8")),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run(input_path, tmp_path)

    assert result == {"qiuzhao": 7, "gongkao": 4}
    qiuzhao = json.loads((tmp_path / "xiaozhaoya_qiuzhao.json").read_text(encoding="utf-8"))
    gongkao = json.loads((tmp_path / "xiaozhaoya_gongkao.json").read_text(encoding="utf-8"))
    assert len(qiuzhao["items"]) == 7
    assert len(gongkao["items"]) == 4
