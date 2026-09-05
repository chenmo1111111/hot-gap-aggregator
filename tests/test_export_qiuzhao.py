from __future__ import annotations

import json

from app.export_qiuzhao import normalize_jobs_payload, write_qiuzhao


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
