from __future__ import annotations

from app.capture_shasha import build_snapshot, infer_written_test_exempt


def _metadata() -> dict:
    names = (
        "公司", "公司行业", "招聘类型", "工作地点", "开始时间", "截止日期",
        "是否免笔试", "岗位", "公告链接", "投递链接", "届次", "学历要求", "备注",
    )
    fields = {f"f{index}": {"name": name, "property": {}} for index, name in enumerate(names)}
    fields["f1"]["property"] = {"options": [
        {"id": "tech", "name": "科技"}, {"id": "soe", "name": "国央企"},
    ]}
    fields["f2"]["property"] = {"options": [{"id": "autumn", "name": "秋招"}]}
    fields["f3"]["property"] = {"options": [{"id": "sz", "name": "深圳"}]}
    fields["f6"]["property"] = {"options": [
        {"id": "exempt", "name": "含免笔试"}, {"id": "required", "name": "需要笔试"},
    ]}
    fields["f10"]["property"] = {"options": [{"id": "2027", "name": "2027届"}]}
    fields["f11"]["property"] = {"options": [{"id": "bachelor", "name": "本科起"}]}
    return {"fieldMap": fields, "recordCount": 2, "tableRev": 1}


def _cell(value: object) -> dict:
    return {"value": value}


def test_build_snapshot_maps_real_shasha_fields_and_written_test_provenance() -> None:
    chunks = [{"recordMap": {
        "rec1": {
            "f0": _cell("中国二十冶"), "f1": _cell(["soe", "tech"]),
            "f2": _cell(["autumn"]), "f3": _cell(["sz"]),
            "f4": _cell(1789747200000), "f5": _cell("2026/10/31"),
            "f6": _cell("exempt"), "f7": _cell("研发工程师"),
            "f8": _cell([{"link": "https://example.test/a"}]),
            "f9": _cell("https://example.test/apply"), "f10": _cell(["2027"]),
            "f11": _cell(["bachelor"]), "f12": _cell(""),
        },
        "rec2": {
            "f0": _cell("示例科技"), "f1": _cell(["tech"]), "f2": _cell(["autumn"]),
            "f3": _cell(["sz"]), "f4": _cell(1789747200000), "f5": _cell("招满为止"),
            "f6": _cell(None), "f7": _cell("算法岗（无需笔试）"), "f8": _cell([]),
            "f9": _cell("https://example.test/b"), "f10": _cell(["2027"]),
            "f11": _cell(["bachelor"]), "f12": _cell(""),
        },
    }}]
    payload = build_snapshot(_metadata(), chunks, {
        "required_fields": [field["name"] for field in _metadata()["fieldMap"].values()],
        "view_id": "view", "min_items": 1, "max_items": 20,
    })
    first, second = payload["items"]
    assert first["company_type"] == "国企"
    assert first["industry"] == "互联网/科技"
    assert first["written_test_requirement"] == "含免笔试"
    assert first["written_test_exempt_source"] == "source_field"
    assert first["deadline_month"] == "2026-10"
    assert second["written_test_requirement"] == "免笔试"
    assert second["written_test_exempt_source"] == "keyword"


def test_unknown_written_test_is_not_invented() -> None:
    assert infer_written_test_exempt("普通研发岗", "无相关说明") == ("", "")
