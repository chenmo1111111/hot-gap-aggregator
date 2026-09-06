import json
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from app.store.database import Database
from app.watchers.xhs_rule_watch import (
    XhsCookieInvalid,
    XhsRuleWatcher,
    article_changed,
    extract_article_metadata,
    merge_xhs_cookies,
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


@pytest.mark.asyncio
async def test_list_push_log_deduplicates_and_filters_irrelevant_titles(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    config.write_text(
        "list_pages:\n  - {name: 规则修订, url: 'https://school.test/list'}\n"
        "title_keywords: [虚拟卡券, 网络工具]\nwatch_articles: []\n",
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
        return "影响虚拟卡券个人店经营，建议立即下架自查。"

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
        "lists": [], "articles": [(article, fixture_body("xhs_rule_article_new.html"))],
    }))
    assert (await watcher.run())["watch_articles"][0]["status"] == "pushed"
    assert (await watcher.run())["watch_articles"][0]["status"] == "unchanged"
    assert len(judgments) == 1
    assert len(delivered) == 1
    assert delivered[0]["priority"] == "highest"
    alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
    assert alerts["items"] == delivered
    stored = database.get_xhs_rule_snapshot(article["url"])
    assert stored and stored["effective_at"] == "2026-09-08"
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


async def async_value(value):
    return value
