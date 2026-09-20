from __future__ import annotations

from app.pipeline.qiuzhao_dedup import deduplicate_qiuzhao_items


def test_exact_cross_source_duplicate_enriches_existing_without_losing_links() -> None:
    existing = {
        "company_name": "某科技有限公司", "position": "算法工程师",
        "apply_url": "https://old.test/apply", "upstream_source": "wanqing_feishu",
    }
    shasha = {
        "company_name": "某科技", "position": "算法工程师",
        "deadline": "2026-10-31", "written_test_requirement": "含免笔试",
        "apply_url": "https://new.test/apply", "upstream_source": "shasha_feishu",
    }
    items, report = deduplicate_qiuzhao_items([existing, shasha])
    assert len(items) == 1
    assert items[0]["deadline"] == "2026-10-31"
    assert items[0]["written_test_requirement"] == "含免笔试"
    assert items[0]["apply_url"] == "https://old.test/apply"
    assert items[0]["extra"]["backup_apply_urls"] == ["https://new.test/apply"]
    assert report.exact_merged_count == 1
    assert report.enriched_by_origin["shasha_feishu"] == 1


def test_fuzzy_position_uses_shared_similarity_and_evidence() -> None:
    rows = [
        {"company_name": "示例集团", "position": "算法研发工程师", "deadline": "2026-10-31", "upstream_source": "jobs"},
        {"company_name": "示例集团有限公司", "position": "算法研发高级工程师", "deadline": "2026-10-31", "industry": "科技", "upstream_source": "shasha_feishu"},
    ]
    items, report = deduplicate_qiuzhao_items(rows)
    assert len(items) == 1
    assert items[0]["industry"] == "科技"
    assert report.fuzzy_merged_count == 1


def test_conflicting_medium_similarity_is_not_merged() -> None:
    rows = [
        {"company_name": "示例集团", "position": "软件开发工程师", "deadline": "2026-10-01", "upstream_source": "jobs"},
        {"company_name": "示例集团", "position": "软件测试工程师", "deadline": "2026-11-01", "upstream_source": "shasha_feishu"},
    ]
    items, report = deduplicate_qiuzhao_items(rows)
    assert len(items) == 2
    assert report.fuzzy_merged_count == 0
