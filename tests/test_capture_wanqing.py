import base64
import gzip
import json

import pytest

from app.capture_wanqing import CaptureError, build_snapshot, decode_gzip_json


FIELDS = {
    "company": "company",
    "company_type": "company_type",
    "batch": "batch",
    "industry": "industry",
    "position": "position",
    "location": "location",
    "education": "education",
    "cohort": "cohort",
    "deadline": "deadline",
    "written_test": "written_test",
    "apply_url": "apply_url",
    "announcement_url": "announcement_url",
    "notes": "notes",
    "updated_at": "updated_at",
}


def _config(*, max_items: int = 2, min_items: int = 1) -> dict:
    return {
        "view_id": "view",
        "source_view": "27届秋招🍁",
        "max_items": max_items,
        "min_items": min_items,
        "fields": FIELDS,
        "filters": {
            "cohort_options": ["2027"],
            "batch_options": ["early", "autumn"],
            "written_test_yes_options": ["yes"],
        },
    }


def _metadata() -> dict:
    return {
        "meta": {"rev": 8092},
        "recordCount": 4,
        "fieldMap": {
            "company_type": {"property": {"options": [
                {"id": "central", "name": "央国企"},
                {"id": "private", "name": "民企"},
            ]}},
            "industry": {"property": {"options": [
                {"id": "manufacturing", "name": "制造业"},
                {"id": "watermark", "name": "婉清学姐冲冲冲的店唯一正版"},
            ]}},
            "cohort": {"property": {"options": [{"id": "2027", "name": "2027届"}]}},
            "batch": {"property": {"options": [
                {"id": "early", "name": "秋招提前批"},
                {"id": "internship", "name": "实习"},
            ]}},
            "written_test": {"property": {"options": [{"id": "yes", "name": "是"}]}},
        }
    }


def _cell(value):
    return {"value": value}


def _record(company: str, position: str, *, updated_at: int, batch: str = "early") -> dict:
    return {
        "company": _cell(company),
        "company_type": _cell(["central"]),
        "batch": _cell([batch]),
        "industry": _cell(["manufacturing", "watermark"]),
        "position": _cell(position),
        "location": _cell("北京"),
        "education": _cell("本科"),
        "cohort": _cell(["2027"]),
        "deadline": _cell("2026-10-01"),
        "written_test": _cell(["yes"]),
        "apply_url": _cell([{"link": "https://example.com/apply"}]),
        "announcement_url": _cell([{"link": "https://example.com/notice"}]),
        "notes": _cell("/"),
        "updated_at": _cell(updated_at),
    }


def test_decode_gzip_json() -> None:
    encoded = base64.b64encode(gzip.compress(json.dumps({"ok": True}).encode())).decode()
    assert decode_gzip_json(encoded) == {"ok": True}


def test_build_snapshot_filters_sorts_deduplicates_and_maps_fields() -> None:
    chunks = [{
        "recordMap": {
            "older": _record("示例公司", "算法岗", updated_at=100),
            "newer": _record("示例公司", "算法岗", updated_at=300),
            "second": _record("另一公司", "研发岗", updated_at=200),
            "ignored": _record("实习公司", "实习岗", updated_at=400, batch="internship"),
        },
        "rankInfo": {"viewRankMap": {"view": {"rankMap": {"newer": "a", "second": "b"}}}},
    }]
    result = build_snapshot(
        _metadata(), chunks, _config(), generated_at="2026-09-06T07:00:00+08:00"
    )

    assert [item["source_record_id"] for item in result["items"]] == ["newer", "second"]
    first = result["items"][0]
    assert first["company_type"] == "国企"
    assert first["industry"] == "制造业"
    assert first["cohort"] == "2027届"
    assert first["written_test"] is True
    assert first["apply_url"] == "https://example.com/apply"
    assert first["notes"] == ""
    assert result["status"]["source_total_records"] == 4
    assert result["status"]["source_matched_records"] == 3


def test_build_snapshot_preserves_old_file_by_rejecting_small_capture() -> None:
    chunks = [{"recordMap": {"one": _record("公司", "岗位", updated_at=1)}}]
    with pytest.raises(CaptureError, match="低于最低要求"):
        build_snapshot(_metadata(), chunks, _config(max_items=500, min_items=450))
