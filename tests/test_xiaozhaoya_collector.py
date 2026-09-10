from __future__ import annotations

from datetime import date

from app.capture_xiaozhaoya import IncrementalStopPolicy, build_snapshot
from app.collectors.xiaozhaoya import resolve_public_url, split_records


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
    assert qiuzhao[0]["company_name"] == "飞腾信息技术有限公司"
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
