from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.capture_xiaozhaoya import IncrementalStopPolicy, build_snapshot
from app.collect_xiaozhaoya import run
from app.collectors.xiaozhaoya import HOME_URL, resolve_public_url, split_records


FIXTURE = Path(__file__).parent / "fixtures" / "xiaozhaoya_recruitment_list.json"


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


def test_resolve_public_url_ignores_bare_site_root() -> None:
    assert resolve_public_url("/") == ""
    assert resolve_public_url("官网：https://jobs.example.com/apply") == "https://jobs.example.com/apply"


def test_incremental_capture_stops_after_two_fully_known_pages() -> None:
    policy = IncrementalStopPolicy({"1", "2", "3"}, cutoff=date(2026, 9, 7))

    assert policy.observe([{"recruitmentId": 1, "updateDate": "2026-09-10"}]) is None
    assert policy.observe([{"recruitmentId": 2, "updateDate": "2026-09-09"}]) == "two_known_pages"


def test_incremental_snapshot_preserves_previous_rows() -> None:
    previous = {
        "total": 2,
        "items": [
            {"recruitmentId": 1, "updateDate": "2026-09-09", "jobTitle": "旧岗位"},
            {"recruitmentId": 2, "updateDate": "2026-09-08", "jobTitle": "保留岗位"},
        ],
    }

    snapshot = build_snapshot(
        [{"recruitmentId": 1, "updateDate": "2026-09-10", "jobTitle": "更新岗位"}],
        total=2,
        previous=previous,
        full=False,
        generated_at="2026-09-10T07:30:00+08:00",
    )

    assert [row["recruitmentId"] for row in snapshot["items"]] == [1, 2]
    assert snapshot["items"][0]["jobTitle"] == "更新岗位"


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
