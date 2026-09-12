from __future__ import annotations

import json

from app.export_qiuzhao import (
    normalize_jobs_payload,
    normalize_snapshot_payload,
    write_qiuzhao,
)


def test_normalize_jobs_payload_preserves_known_fields_without_inventing_deadline() -> None:
    payload = {
        "generated_at": "2026-09-05T02:00:00+00:00",
        "status": {"source": "jobs", "status": "ok", "item_count": 1},
        "items": [{
            "title_zh": "AI 研发工程师", "url": "https://example.com/apply",
            "summary_zh": "负责模型研发", "extra": {
                "company": "示例公司", "city": "北京", "keywords_hit": ["AI for Science", "生物信息"],
            },
        }],
    }
    output = normalize_jobs_payload(payload)
    assert output["source"] == "qiuzhao"
    assert output["status"]["upstream_source"] == "jobs"
    assert output["items"] == [{
        "company_name": "示例公司", "company_type": "", "industry": "AI for Science、生物信息",
        "position": "AI 研发工程师", "location": "北京", "education": "", "cohort": "",
        "deadline": "", "written_test": None, "apply_url": "https://example.com/apply",
        "announcement_url": "https://example.com/apply", "notes": "负责模型研发", "upstream_source": "jobs",
    }]


def test_write_qiuzhao_creates_atomically_from_jobs_json(tmp_path) -> None:
    (tmp_path / "jobs.json").write_text(json.dumps({
        "generated_at": "2026-09-05T02:00:00+00:00", "status": {},
        "items": [{"title": "岗位", "extra": {"company": "公司"}}],
    }, ensure_ascii=False), encoding="utf-8")
    result = write_qiuzhao(tmp_path)
    written = json.loads((tmp_path / "qiuzhao.json").read_text(encoding="utf-8"))
    assert result == written
    assert written["items"][0]["company_name"] == "公司"
    assert written["items"][0]["position"] == "岗位"


def test_normalize_snapshot_payload_preserves_standardized_rows() -> None:
    payload = {
        "generated_at": "2026-09-06T05:00:00.000Z",
        "status": {"source_view": "27届秋招🍁"},
        "items": [{
            "company_name": "示例公司",
            "position": "研发工程师",
            "apply_url": "https://example.com/apply",
            "source_record_id": "rec_example",
        }],
    }
    output = normalize_snapshot_payload(payload)
    assert output["status"]["item_count"] == 1
    assert output["status"]["upstream_source"] == "wanqing_feishu"
    assert output["items"] == payload["items"]


def test_write_qiuzhao_merges_wanqing_snapshot_with_collected_jobs(tmp_path) -> None:
    (tmp_path / "jobs.json").write_text(json.dumps({
        "items": [{"title": "旧岗位", "extra": {"company": "旧公司"}}],
    }, ensure_ascii=False), encoding="utf-8")
    snapshot = {
        "generated_at": "2026-09-06T05:00:00.000Z",
        "items": [{"company_name": "新公司", "position": "新岗位"}],
    }
    (tmp_path / "qiuzhao_wanqing.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )

    result = write_qiuzhao(tmp_path)

    assert result["items"][0] == {
        **snapshot["items"][0], "company_type": "机构性质待核",
        "extra": {"record_kind": "秋招", "organization_type": "机构性质待核"},
        "upstream_source": "wanqing_feishu",
    }
    assert result["items"][1]["company_name"] == "旧公司"
    assert result["status"]["upstream_source"] == "wanqing_feishu+jobs"
    written = json.loads((tmp_path / "qiuzhao.json").read_text(encoding="utf-8"))
    assert written == result


def test_write_qiuzhao_prefers_manual_row_for_same_company_and_position(tmp_path) -> None:
    (tmp_path / "jobs.json").write_text(json.dumps({
        "items": [{
            "title": "研发工程师",
            "url": "https://jobs.example/collected",
            "extra": {"company": "示例公司", "city": "上海"},
        }],
    }, ensure_ascii=False), encoding="utf-8")
    manual = {
        "company_name": "示例公司",
        "position": "研发工程师",
        "location": "北京",
        "apply_url": "https://jobs.example/manual",
    }
    (tmp_path / "qiuzhao_wanqing.json").write_text(
        json.dumps({"items": [manual]}, ensure_ascii=False), encoding="utf-8"
    )

    result = write_qiuzhao(tmp_path)

    assert result["items"] == [{
        **manual, "company_type": "机构性质待核",
        "extra": {"record_kind": "秋招", "organization_type": "机构性质待核"},
        "upstream_source": "wanqing_feishu",
    }]


def test_write_qiuzhao_merges_xiaozhaoya_snapshot(tmp_path) -> None:
    (tmp_path / "jobs.json").write_text(json.dumps({
        "items": [{"title": "已有岗位", "extra": {"company": "已有公司"}}],
    }, ensure_ascii=False), encoding="utf-8")
    row = {
        "company_name": "校招鸭公司", "position": "研发岗",
        "source_record_id": "xiaozhaoya:1", "source_label": "校招鸭",
    }
    (tmp_path / "xiaozhaoya_qiuzhao.json").write_text(
        json.dumps({"items": [row]}, ensure_ascii=False), encoding="utf-8",
    )

    result = write_qiuzhao(tmp_path)

    assert result["items"][0] == {
        **row, "company_type": "机构性质待核",
        "extra": {"record_kind": "秋招", "organization_type": "机构性质待核"},
        "upstream_source": "xiaozhaoya",
    }
    assert "xiaozhaoya" in result["status"]["upstream_source"]


def test_daily_wanqing_wins_foundation_duplicate_and_expired_foundation_is_pruned(tmp_path) -> None:
    daily = {
        "company_name": "同一公司", "position": "研发岗",
        "apply_url": "https://daily.example/apply", "deadline": "2099-12-31",
    }
    (tmp_path / "qiuzhao_wanqing.json").write_text(
        json.dumps({"items": [daily]}, ensure_ascii=False), encoding="utf-8"
    )
    foundation = [
        {
            "company_name": "同一公司", "position": "研发岗",
            "apply_url": "https://foundation.example/apply", "deadline": "2099-12-31",
            "source_record_id": "xiaozhaoya:1",
        },
        {
            "company_name": "过期公司", "position": "旧岗位",
            "deadline": "2020-01-01", "source_record_id": "xiaozhaoya:2",
        },
    ]
    (tmp_path / "xiaozhaoya_qiuzhao.json").write_text(
        json.dumps({"items": foundation}, ensure_ascii=False), encoding="utf-8"
    )

    result = write_qiuzhao(tmp_path)

    assert result["items"] == [{
        **daily, "company_type": "机构性质待核",
        "extra": {"record_kind": "秋招", "organization_type": "机构性质待核"},
        "upstream_source": "wanqing_feishu",
    }]


def test_write_qiuzhao_routes_wanqing_public_rows_to_gongkao_sidecar(tmp_path) -> None:
    (tmp_path / "qiuzhao_wanqing.json").write_text(json.dumps({"items": [
        {
            "company_name": "中国社科院考古研究所", "company_type": "其他",
            "position": "科研岗", "source_record_id": "public-1",
            "announcement_url": "https://example.com/public-1",
        },
        {"company_name": "示例公司", "company_type": "民企", "position": "研发岗"},
    ]}, ensure_ascii=False), encoding="utf-8")

    result = write_qiuzhao(tmp_path)
    routed = json.loads((tmp_path / "purchased_gongkao.json").read_text(encoding="utf-8"))

    assert [row["company_name"] for row in result["items"]] == ["示例公司"]
    assert routed["status"]["route_counts"]["wanqing_feishu"] == 1
    assert routed["items"][0]["extra"]["unit"] == "中国社科院考古研究所"


def test_write_qiuzhao_filters_event_noise_from_server_jobs(tmp_path) -> None:
    (tmp_path / "server-jobs.json").write_text(json.dumps({
        "items": [
            {"title": "某集团 宣讲会", "extra": {"company": "某集团"}},
            {"title": "某集团 研发工程师", "extra": {"company": "某集团"}},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    result = write_qiuzhao(tmp_path)

    assert [row["position"] for row in result["items"]] == ["某集团 研发工程师"]
