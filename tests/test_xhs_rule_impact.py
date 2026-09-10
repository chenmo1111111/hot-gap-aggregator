from datetime import date

import pytest

from app.pipeline.xhs_rule_impact import (
    DeepSeekRuleImpactAnalyzer,
    MANUAL_LINK_CHECK,
    format_impact_notification,
)


def snapshot(*, stale=False, items=True):
    return {
        "stale": stale,
        "items": [{"item_id": "1", "title": "电子题库", "category_path": "教育 > 电子资源"}] if items else [],
    }


def test_recently_effective_rule_is_still_marked_urgent() -> None:
    analysis = {
        "verdict": "review", "summary": "核对", "affected_items": [],
        "action_plan": [], "manual_checks": [], "deadline": "2026-09-03",
    }
    _, message, _ = format_impact_notification(
        "类目规则", "2026-08-01", "2026-09-01", "2026-09-03", analysis, 3,
        urgent_within_days=7, today=date(2026, 9, 9),
    )
    assert "紧急" in message


@pytest.mark.asyncio
async def test_no_change_message() -> None:
    async def caller(_system, _user):
        return '{"verdict":"no_change","summary":"无影响","affected_items":[],"action_plan":[],"manual_checks":[],"deadline":"2026-10-01"}'

    result = await DeepSeekRuleImpactAnalyzer(caller).analyze(
        {"effective_at": "2026-10-01", "external_links": []}, snapshot(),
    )
    title, message, priority = format_impact_notification(
        "定向准入", "2026-09-01", "2026-09-09", "2026-10-01", result, 1,
    )
    assert "不影响你在售的 1 个商品" in message
    assert priority == "normal" and "定向准入" in title


@pytest.mark.asyncio
async def test_action_required_message_lists_item_and_plan() -> None:
    async def caller(_system, _user):
        return '''{"verdict":"action_required","summary":"需下架","affected_items":[{"item_id":"1","title":"电子题库","category":"教育","risk":"high","why":"类目下线","suggestion":"立即下架"}],"action_plan":["今天完成下架"],"manual_checks":[],"deadline":"2026-09-12"}'''

    result = await DeepSeekRuleImpactAnalyzer(caller).analyze(
        {"effective_at": "2026-09-12", "external_links": []}, snapshot(),
    )
    _, message, priority = format_impact_notification(
        "类目规则", "2026-09-01", "2026-09-09", "2026-09-12", result, 1,
        today=date(2026, 9, 9),
    )
    assert "🔴需要改动" in message
    assert "电子题库」高风险：类目下线 → 立即下架" in message
    assert "1. 今天完成下架" in message and "紧急" in message
    assert priority == "highest"


@pytest.mark.asyncio
async def test_stale_or_empty_shop_forces_review() -> None:
    async def caller(_system, _user):
        return '{"verdict":"no_change","summary":"无影响","affected_items":[],"action_plan":[],"manual_checks":[],"deadline":""}'

    for shop in (snapshot(stale=True), snapshot(items=False)):
        result = await DeepSeekRuleImpactAnalyzer(caller).analyze(
            {"effective_at": "", "external_links": []}, shop,
        )
        assert result["verdict"] == "review"
        assert "商品信息过期/未拉取到" in result["summary"]


@pytest.mark.asyncio
async def test_deepseek_failure_returns_review_without_raising() -> None:
    async def caller(_system, _user):
        raise RuntimeError("provider down")

    result = await DeepSeekRuleImpactAnalyzer(caller).analyze(
        {"effective_at": "2026-10-01", "external_links": []}, snapshot(),
    )
    assert result["verdict"] == "review"
    assert result["summary"] == "AI 分析失败，请人工核对在售商品"


@pytest.mark.asyncio
async def test_unresolved_external_document_requires_manual_check_and_blocks_no_change() -> None:
    async def caller(_system, _user):
        return '{"verdict":"no_change","summary":"无影响","affected_items":[],"action_plan":[],"manual_checks":[],"deadline":"2026-10-01"}'

    link = "https://doc.weixin.qq.com/sheet/demo?scode=secret-value&tab=000001"
    result = await DeepSeekRuleImpactAnalyzer(caller).analyze(
        {"effective_at": "2026-10-01", "external_links": [link]}, snapshot(),
    )
    assert result["verdict"] == "review"
    assert any(MANUAL_LINK_CHECK in row and "sheet/demo" in row for row in result["manual_checks"])
    assert all("secret-value" not in row for row in result["manual_checks"])


@pytest.mark.asyncio
async def test_fetched_external_document_is_used_and_does_not_force_review() -> None:
    prompts: list[str] = []

    async def caller(_system, user):
        prompts.append(user)
        return '{"verdict":"no_change","summary":"当前店铺类型仍可售","affected_items":[],"action_plan":[],"manual_checks":[],"deadline":"2026-10-01"}'

    raw_link = "https://doc.weixin.qq.com/sheet/demo?scode=secret-value&tab=000001"
    rule = {
        "effective_at": "2026-10-01",
        "external_links": [raw_link],
        "external_documents": [{
            "source_id": "doc.weixin.qq.com/sheet/demo?tab=000001",
            "source_url": "https://doc.weixin.qq.com/sheet/demo?tab=000001",
            "title": "类目可售明细",
            "stale": False,
            "content_text": "教育 > 电子资源 > 题库：普通企业店可售",
        }],
    }
    result = await DeepSeekRuleImpactAnalyzer(caller).analyze(rule, snapshot())
    assert result["verdict"] == "no_change"
    assert result["manual_checks"] == []
    assert "普通企业店可售" in prompts[0]
    assert "secret-value" not in prompts[0]
