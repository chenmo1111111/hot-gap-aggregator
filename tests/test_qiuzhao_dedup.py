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


def test_codefather_complete_position_array_enriches_matching_existing_role() -> None:
    rows = [
        {
            "company_name": "英飞源技术", "position": "算法工程师",
            "location": "深圳", "cohort": "2027届", "upstream_source": "wanqing_feishu",
        },
        {
            "company_name": "英飞源技术", "position": "嵌入式软件工程师、算法工程师、硬件工程师",
            "location": "南京、深圳、珠海", "cohort": "2027届",
            "apply_url": "https://mp.weixin.qq.com/s/example", "upstream_source": "codefather",
            "extra": {"position_list": ["嵌入式软件工程师", "算法工程师", "硬件工程师"]},
        },
    ]
    items, report = deduplicate_qiuzhao_items(rows)
    assert len(items) == 1
    assert items[0]["apply_url"] == "https://mp.weixin.qq.com/s/example"
    assert items[0]["extra"]["position_list"] == ["嵌入式软件工程师", "算法工程师", "硬件工程师"]
    assert report.enriched_by_origin["codefather"] == 1
    assert report.new_by_origin["codefather"] == 0
