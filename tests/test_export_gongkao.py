from __future__ import annotations

import json

from app.export_gongkao import merge_gongkao_payloads, write_gongkao


def _item(identifier: str, title: str, url: str, province: str = "山东") -> dict:
    return {
        "title": title,
        "url": url,
        "extra": {
            "id": identifier, "province": province,
            "exam_type": "事业单位", "has_announcement_structure": True,
        },
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
    assert output["status"]["sheet_merged_count"] == 2
    assert output["items"][0]["url"] == "https://b.test/2"
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


def test_merge_adds_server_watchers_before_sheet_and_assigns_stable_id() -> None:
    base = {"items": [_item("base", "基础公告", "https://a.test/1")]}
    server = {
        "items": [{
            "title": "辽宁定向选调公告",
            "url": "https://rst.ln.gov.cn/xuandiao/1",
            "extra": {
                "subsource": "xuandiao", "province": "辽宁",
                "exam_type": "选调生", "government_source": True,
            },
        }]
    }
    sheet = {"items": [_item("sheet", "重复的表格公告", "https://rst.ln.gov.cn/xuandiao/1")]}

    output = merge_gongkao_payloads(base, sheet, server)

    assert len(output["items"]) == 2
    watcher = output["items"][1]
    assert watcher["extra"]["id"].startswith("watcher:")
    assert watcher["extra"]["sub"] == "announcement"
    assert output["status"]["server_added_count"] == 1
    assert output["status"]["sheet_duplicate_count"] == 1
    assert output["status"]["sheet_merged_count"] == 0


def test_sheet_official_url_overrides_duplicate_fenbi_links() -> None:
    title = "2027年度内蒙古自治区事业单位公开招聘工作人员公告"
    base = {
        "items": [
            {
                "title": title,
                "url": "https://hera-webapp.fenbi.com/api/website/article/detail?id=468944",
                "extra": {
                    "id": 468944,
                    "sub": "announcement",
                    "province": "内蒙古",
                    "endSignUpTime": 1789635600000,
                },
            },
            {
                "title": title,
                "url": "https://www.fenbi.com/page/exam-timeline-detail/908859",
                "extra": {"id": 908859, "sub": "timeline", "province": "内蒙古"},
            },
        ]
    }
    official = "http://www.impta.com.cn/shiyedanwei/202698223431.asp"
    sheet = {
        "items": [{
            "title": title,
            "url": official,
            "published_at": "2026-09-08",
            "extra": {
                "id": "gongkao-sheet:official",
                "subsource": "feishu_sheet",
                "province": "内蒙古自治区",
            },
        }]
    }

    output = merge_gongkao_payloads(base, sheet)

    assert [item["url"] for item in output["items"]] == [official, official]
    assert output["items"][0]["extra"]["id"] == 468944
    assert output["items"][0]["extra"]["endSignUpTime"] == 1789635600000
    assert output["items"][0]["extra"]["preferred_link_source"] == "feishu_sheet"
    assert output["items"][0]["extra"]["replaced_urls"] == [
        "https://hera-webapp.fenbi.com/api/website/article/detail?id=468944"
    ]
    assert output["items"][1]["extra"]["replaced_urls"] == [
        "https://www.fenbi.com/page/exam-timeline-detail/908859"
    ]
    assert output["status"]["sheet_added_count"] == 0
    assert output["status"]["sheet_duplicate_count"] == 1
    assert output["status"]["sheet_merged_count"] == 1


def test_government_link_is_not_overwritten_by_sheet_or_watcher_snapshot() -> None:
    title = "湖南省2026年省直事业单位第四次公开招聘公告"
    official = "https://rst.hunan.gov.cn/rst/xxgk/zpzl/sydwzp/202609/t20260908_34059489.html"
    base = {
        "items": [{
            "title": title,
            "url": official,
            "published_at": "2026-09-08",
            "extra": {
                "id": "gov:hunan-4", "province": "湖南", "exam_type": "事业单位",
                "government_source": True,
            },
        }]
    }
    watcher = {
        "items": [{
            "title": title,
            "url": "https://mp.weixin.qq.com/s/old-watcher-copy",
            "extra": {"province": "详见正文", "exam_type": "事业单位"},
        }]
    }
    sheet = {
        "items": [{
            "title": title,
            "url": "https://mp.weixin.qq.com/s/old-sheet-copy",
            "extra": {"province": "湖南省", "exam_type": "事业单位"},
        }]
    }

    output = merge_gongkao_payloads(base, sheet, watcher)

    assert len(output["items"]) == 1
    assert output["items"][0]["url"] == official
    assert output["items"][0]["extra"]["province"] == "湖南"
    assert output["status"]["server_merged_count"] == 0
    assert output["status"]["sheet_merged_count"] == 0
