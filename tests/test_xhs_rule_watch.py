import json
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from app.store.database import Database
from app.watchers.xhs_rule_watch import (
    RULE_CHANGE_PROMPT,
    XhsCookieInvalid,
    XhsRuleWatcher,
    article_changed,
    captures_match,
    extract_article_metadata,
    extract_rule_text_window,
    merge_xhs_cookies,
    parse_model_decision,
    parse_rule_list_text,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_body(name: str) -> str:
    tree = HTMLParser((FIXTURES / name).read_text(encoding="utf-8"))
    return tree.body.text(separator="\n", strip=True)


def test_cookie_sources_merge_with_json_precedence_and_playwright_fields() -> None:
    json_cookie = json.dumps([
        {
            "name": "web_session", "value": "json-wins", "domain": ".xiaohongshu.com",
            "path": "/", "secure": False, "httpOnly": True, "sameSite": None,
            "expirationDate": 2_000_000_000.9,
        },
        {"name": "id_token", "value": "secret", "domain": "school.xiaohongshu.com", "sameSite": "lax"},
    ])
    merged = merge_xhs_cookies(json_cookie, "a1=doc-a1; web_session=doc-loses; gid=a=b")
    by_name = {cookie["name"]: cookie for cookie in merged}
    assert set(by_name) == {"web_session", "id_token", "a1", "gid"}
    assert by_name["web_session"]["value"] == "json-wins"
    assert by_name["web_session"]["secure"] is True
    assert by_name["web_session"]["httpOnly"] is True
    assert by_name["web_session"]["expires"] == 2_000_000_000
    assert "sameSite" not in by_name["web_session"]
    assert by_name["id_token"]["sameSite"] == "Lax"
    assert by_name["gid"]["value"] == "a=b"


def test_list_regex_splits_rules_and_article_metadata_extracts_dates_and_document() -> None:
    rows = parse_rule_list_text(fixture_body("xhs_rule_list.html"), "规则修订")
    assert [(row["kind"], row["title"], row["date"]) for row in rows] == [
        ("修订", "小红书虚拟卡券商品发布规范", "2026年09月05日"),
        ("新增", "网络工具类目定向准入规则", "2026年09月04日"),
        ("修订", "生鲜食品运输规范", "2026年09月03日"),
    ]
    old = extract_article_metadata(fixture_body("xhs_rule_article_old.html"))
    new = extract_article_metadata(fixture_body("xhs_rule_article_new.html"))
    assert old["announced_at"] == "2026-08-01"
    assert old["effective_at"] == "2026-08-08"
    assert old["revised_at"] == "2026-08-01"
    assert old["document_url"].endswith("e3_demo_old")
    assert old["content_hash"] != new["content_hash"]
    assert article_changed(old, new)
    assert not article_changed(old, old)
    assert captures_match(old, old)
    assert not captures_match(old, new)
    assert extract_rule_text_window(fixture_body("xhs_rule_article_old.html")).startswith("本规则于")
    assert parse_model_decision('{"changed": false, "impact": ""}') == (False, "")


@pytest.mark.asyncio
async def test_list_push_log_deduplicates_and_filters_irrelevant_titles(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    config.write_text(
        "list_pages:\n  - {name: 规则修订, url: 'https://school.test/list'}\n"
        "title_keywords: [虚拟卡券, 网络工具]\n"
        "focus_keywords: [虚拟卡券]\nwatch_articles: []\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "watch.db")
    delivered: list[dict[str, str]] = []

    async def notifier(alert: dict[str, str]) -> dict[str, str]:
        delivered.append(alert)
        return {"feishu": "ok"}

    alerts_path = tmp_path / "alerts.json"
    watcher = XhsRuleWatcher(database, config, notifier=notifier, alerts_path=alerts_path)
    page = {"name": "规则修订", "url": "https://school.test/list"}
    monkeypatch.setattr(watcher, "_scrape", lambda _config: async_value({
        "lists": [(page, fixture_body("xhs_rule_list.html"))], "articles": [],
    }))
    first = await watcher.run()
    second = await watcher.run()
    assert first["list_pages"][0] == {"name": "规则修订", "status": "pushed", "item_count": 2, "pushed": 2}
    assert second["list_pages"][0]["status"] == "unchanged"
    assert len(delivered) == 2
    assert delivered[0]["priority"] == "highest"
    assert "重点关注" in delivered[0]["summary"]
    assert delivered[1]["priority"] == "normal"
    alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
    assert len(alerts["items"]) == 2
    assert {item["id"] for item in alerts["items"]} == {item["id"] for item in delivered}
    database.close()


@pytest.mark.asyncio
async def test_article_baseline_then_diff_judgment_and_one_push(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    config.write_text(
        "list_pages: []\nwatch_articles:\n"
        "  - {name: 定向准入, url: 'https://school.test/detail/1'}\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "watch.db")
    judgments: list[str] = []
    delivered: list[dict[str, str]] = []

    async def judge(prompt: str) -> str:
        judgments.append(prompt)
        return '{"changed": true, "impact": "影响虚拟卡券个人店经营，建议立即下架自查。"}'

    async def notifier(alert: dict[str, str]) -> dict[str, str]:
        delivered.append(alert)
        return {"feishu": "ok"}

    alerts_path = tmp_path / "alerts.json"
    watcher = XhsRuleWatcher(database, config, judge=judge, notifier=notifier, alerts_path=alerts_path)
    article = {"name": "定向准入", "url": "https://school.test/detail/1"}
    monkeypatch.setattr(watcher, "_scrape", lambda _config: async_value({
        "lists": [], "articles": [(article, fixture_body("xhs_rule_article_old.html"))],
    }))
    assert (await watcher.run())["watch_articles"][0]["status"] == "baseline"

    monkeypatch.setattr(watcher, "_scrape", lambda _config: async_value({
        "lists": [], "articles": [(
            article, fixture_body("xhs_rule_article_new.html"), fixture_body("xhs_rule_article_new.html"),
        )],
    }))
    assert (await watcher.run())["watch_articles"][0]["status"] == "pushed"
    assert (await watcher.run())["watch_articles"][0]["status"] == "unchanged"
    assert len(judgments) == 1
    assert "电子资源" in RULE_CHANGE_PROMPT and "教育" in RULE_CHANGE_PROMPT
    assert '"changed": true' in RULE_CHANGE_PROMPT
    assert len(delivered) == 1
    assert delivered[0]["priority"] == "highest"
    alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
    assert alerts["items"] == delivered
    stored = database.get_xhs_rule_snapshot(article["url"])
    assert stored and stored["effective_at"] == "2026-09-08"
    database.close()


@pytest.mark.asyncio
async def test_unchanged_structured_fields_ignore_body_noise_without_model_or_push(tmp_path) -> None:
    database = Database(tmp_path / "watch.db")
    judgments: list[str] = []
    delivered: list[dict[str, str]] = []

    async def judge(prompt: str) -> str:
        judgments.append(prompt)
        return '{"changed": true, "impact": "不应调用"}'

    async def notifier(alert: dict[str, str]) -> dict[str, str]:
        delivered.append(alert)
        return {"feishu": "ok"}

    watcher = XhsRuleWatcher(
        database, tmp_path / "unused.yaml", judge=judge, notifier=notifier,
        alerts_path=tmp_path / "alerts.json",
    )
    article = {"name": "定向准入", "url": "https://school.test/detail/1"}
    old = fixture_body("xhs_rule_article_old.html")
    assert (await watcher._process_article(article, old))["status"] == "baseline"
    noisy = old + " 登录问候随机变化 2026-09-09 12:00"
    assert (await watcher._process_article(article, noisy))["status"] == "unchanged"
    assert judgments == []
    assert delivered == []
    database.close()


@pytest.mark.asyncio
async def test_changed_but_two_captures_disagree_is_not_pushed(tmp_path) -> None:
    database = Database(tmp_path / "watch.db")
    delivered: list[dict[str, str]] = []

    async def notifier(alert: dict[str, str]) -> dict[str, str]:
        delivered.append(alert)
        return {"feishu": "ok"}

    watcher = XhsRuleWatcher(
        database, tmp_path / "unused.yaml", notifier=notifier,
        alerts_path=tmp_path / "alerts.json",
    )
    article = {"name": "定向准入", "url": "https://school.test/detail/1"}
    old, new = fixture_body("xhs_rule_article_old.html"), fixture_body("xhs_rule_article_new.html")
    await watcher._process_article(article, old)
    result = await watcher._process_article(article, new, new + " 页面随机尾巴")
    assert result["status"] == "confirmation-mismatch"
    assert delivered == []
    assert database.get_xhs_rule_snapshot(article["url"])["effective_at"] == "2026-08-08"
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_result", "expected_status"),
    [
        ('{"changed": false, "impact": ""}', "changed-no-impact"),
        ("不是 JSON", "model-degraded"),
    ],
)
async def test_model_false_or_invalid_response_does_not_push(tmp_path, model_result, expected_status) -> None:
    database = Database(tmp_path / f"{expected_status}.db")
    delivered: list[dict[str, str]] = []

    async def judge(_prompt: str) -> str:
        return model_result

    async def notifier(alert: dict[str, str]) -> dict[str, str]:
        delivered.append(alert)
        return {"feishu": "ok"}

    watcher = XhsRuleWatcher(
        database, tmp_path / "unused.yaml", judge=judge, notifier=notifier,
        alerts_path=tmp_path / "alerts.json",
    )
    article = {"name": "定向准入", "url": "https://school.test/detail/1"}
    old, new = fixture_body("xhs_rule_article_old.html"), fixture_body("xhs_rule_article_new.html")
    await watcher._process_article(article, old)
    assert (await watcher._process_article(article, new, new))["status"] == expected_status
    assert delivered == []
    database.close()


@pytest.mark.asyncio
async def test_model_failure_does_not_push_or_advance_snapshot(tmp_path) -> None:
    database = Database(tmp_path / "watch.db")
    delivered: list[dict[str, str]] = []

    async def judge(_prompt: str) -> str:
        raise RuntimeError("provider unavailable")

    async def notifier(alert: dict[str, str]) -> dict[str, str]:
        delivered.append(alert)
        return {"feishu": "ok"}

    watcher = XhsRuleWatcher(
        database, tmp_path / "unused.yaml", judge=judge, notifier=notifier,
        alerts_path=tmp_path / "alerts.json",
    )
    article = {"name": "定向准入", "url": "https://school.test/detail/1"}
    old, new = fixture_body("xhs_rule_article_old.html"), fixture_body("xhs_rule_article_new.html")
    await watcher._process_article(article, old)
    assert (await watcher._process_article(article, new, new))["status"] == "model-degraded"
    assert delivered == []
    assert database.get_xhs_rule_snapshot(article["url"])["effective_at"] == "2026-08-08"
    database.close()


@pytest.mark.asyncio
async def test_true_change_push_uses_stable_container_hash_key(tmp_path) -> None:
    database = Database(tmp_path / "watch.db")

    async def judge(_prompt: str) -> str:
        return '```json\n{"changed": true, "impact": "教育类目改为定向准入"}\n```'

    async def notifier(_alert: dict[str, str]) -> dict[str, str]:
        return {"feishu": "ok"}

    watcher = XhsRuleWatcher(
        database, tmp_path / "unused.yaml", judge=judge, notifier=notifier,
        alerts_path=tmp_path / "alerts.json",
    )
    article = {"name": "定向准入", "url": "https://school.test/detail/1"}
    old, new = fixture_body("xhs_rule_article_old.html"), fixture_body("xhs_rule_article_new.html")
    await watcher._process_article(article, old)
    result = await watcher._process_article(article, new, new)
    assert result["status"] == "pushed"
    content_hash = extract_article_metadata(new)["content_hash"]
    event_key = f"xhs-rule:article:{content_hash}"
    assert database.unseen_push_events([event_key]) == set()
    assert "公示 2026-08-01→2026-09-01" in result["summary"]
    assert "生效 2026-08-08→2026-09-08" in result["summary"]
    database.close()


@pytest.mark.asyncio
async def test_cookie_failure_alerts_once_without_raising(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    config.write_text("list_pages: []\nwatch_articles: []\n", encoding="utf-8")
    database = Database(tmp_path / "watch.db")
    delivered: list[dict[str, str]] = []

    async def notifier(alert: dict[str, str]) -> dict[str, str]:
        delivered.append(alert)
        return {"bark": "ok"}

    async def expired(_config):
        raise XhsCookieInvalid("login verification failed", status="degraded")

    alerts_path = tmp_path / "alerts.json"
    watcher = XhsRuleWatcher(database, config, notifier=notifier, alerts_path=alerts_path)
    monkeypatch.setattr(watcher, "_scrape", expired)
    assert (await watcher.run())["status"] == "degraded"
    assert (await watcher.run())["status"] == "degraded"
    assert len(delivered) == 1
    assert "重新导出" in delivered[0]["summary"]
    alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
    assert alerts["items"] == delivered
    database.close()


@pytest.mark.asyncio
async def test_manual_analyze_path_refreshes_shop_and_pushes_with_marker(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    config.write_text(
        "list_pages: []\nwatch_articles:\n"
        "  - {name: 类目明细, url: 'https://school.xiaohongshu.com/rule/detail/26/2981'}\n"
        "impact_analysis:\n  enabled: true\n  shop_manage_url: 'https://ark.test/items'\n  urgent_within_days: 7\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "watch.db")
    delivered = []
    calls = []

    async def refresh(settings):
        calls.append(("shop", settings["shop_manage_url"]))
        return {"stale": False, "items": [{"item_id": "1", "title": "电子题库"}]}

    async def analyze(rule, shop):
        calls.append(("impact", rule["col_id"], len(shop["items"])))
        return {
            "verdict": "review", "summary": "建议核对", "affected_items": [],
            "action_plan": ["核对类目"], "manual_checks": [], "deadline": rule["effective_at"],
        }

    async def notifier(alert):
        delivered.append(alert)
        return {"feishu": "ok"}

    watcher = XhsRuleWatcher(
        database, config, notifier=notifier, alerts_path=tmp_path / "alerts.json",
        shop_refresher=refresh, impact_analyzer=analyze,
    )
    article = {"name": "类目明细", "url": "https://school.xiaohongshu.com/rule/detail/26/2981"}
    monkeypatch.setattr(watcher, "_scrape", lambda _config: async_value({
        "lists": [], "articles": [(article, fixture_body("xhs_rule_article_new.html"), None)],
    }))
    result = await watcher.analyze("26/2981")
    assert result["status"] == "pushed" and result["manual"] is True
    assert calls == [("shop", "https://ark.test/items"), ("impact", "26/2981", 1)]
    assert "【手动触发】" in delivered[0]["summary"]
    assert delivered[0]["impact_analysis"]["verdict"] == "review"
    database.close()


@pytest.mark.asyncio
async def test_shop_impact_runs_only_after_confirmed_material_change(tmp_path) -> None:
    database = Database(tmp_path / "watch.db")
    calls = []

    async def judge(_prompt):
        return '{"changed": true, "impact": "电子资源类目限制改变"}'

    async def refresh(_settings):
        calls.append("shop")
        return {"stale": False, "items": [{"item_id": "1", "title": "题库"}]}

    async def analyze(_rule, _shop):
        calls.append("impact")
        return {
            "verdict": "review", "summary": "核对", "affected_items": [],
            "action_plan": [], "manual_checks": [], "deadline": "2026-09-08",
        }

    async def notifier(_alert):
        return {"feishu": "ok"}

    watcher = XhsRuleWatcher(
        database, tmp_path / "unused.yaml", judge=judge, notifier=notifier,
        alerts_path=tmp_path / "alerts.json", shop_refresher=refresh, impact_analyzer=analyze,
    )
    watcher._runtime_config = {
        "impact_analysis": {"enabled": True, "shop_manage_url": "https://ark.test/items"},
    }
    article = {"name": "类目规则", "url": "https://school.test/rule/detail/26/2981"}
    old, new = fixture_body("xhs_rule_article_old.html"), fixture_body("xhs_rule_article_new.html")
    await watcher._process_article(article, old)
    await watcher._process_article(article, old)
    assert calls == []
    await watcher._process_article(article, new, new + " 页面随机尾巴")
    assert calls == []
    result = await watcher._process_article(article, new, new)
    assert result["status"] == "pushed"
    assert calls == ["shop", "impact"]
    database.close()


async def async_value(value):
    return value
