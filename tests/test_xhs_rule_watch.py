import json
from datetime import date
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from app.store.database import Database
from app.watchers.xhs_rule_watch import (
    XhsCookieInvalid,
    XhsRuleWatcher,
    detail_url_from_text,
    extract_article_metadata,
    extract_external_links,
    extract_rule_text_window,
    merge_xhs_cookies,
    parse_published_date,
    parse_rule_list_text,
)


FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 9)


def fixture_body(name: str) -> str:
    tree = HTMLParser((FIXTURES / name).read_text(encoding="utf-8"))
    return tree.body.text(separator="\n", strip=True)


def write_config(path: Path) -> None:
    path.write_text(
        "list_pages:\n  - {name: 规则修订, url: 'https://school.test/list/17'}\n"
        "title_keywords: [类目, 卡券, 规则]\n"
        "watch_articles:\n"
        "  - {name: 月度类目规则, url: 'https://school.xiaohongshu.com/rule/detail/26/2981'}\n"
        "impact_analysis:\n  enabled: true\n  shop_manage_url: 'https://ark.test/items'\n"
        "  urgent_within_days: 7\n",
        encoding="utf-8",
    )


def rule(rule_id: str, published_at: str, title: str = "电子资源类目规则") -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "published_at": published_at,
        "title": title,
        "url": f"https://school.xiaohongshu.com/rule/detail/{rule_id}",
        "date": published_at,
        "kind": "修订",
        "prefix": "关于修订",
        "list_name": "规则修订",
        "list_url": "https://school.test/list/17",
    }


def scrape_result(rows: list[dict[str, str]], *, complete: bool = True):
    return rows, [{
        "name": "规则修订", "status": "ok" if complete else "degraded",
        "item_count": len(rows), "resolved": len(rows), "unresolved": 0,
    }], complete


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


def test_list_and_article_parsers_extract_dates_ids_and_links() -> None:
    rows = parse_rule_list_text(fixture_body("xhs_rule_list.html"), "规则修订")
    assert [(row["kind"], row["title"], row["date"]) for row in rows] == [
        ("修订", "小红书虚拟卡券商品发布规范", "2026年09月05日"),
        ("新增", "网络工具类目定向准入规则", "2026年09月04日"),
        ("修订", "生鲜食品运输规范", "2026年09月03日"),
    ]
    assert parse_published_date("2026年09月05日") == date(2026, 9, 5)
    assert parse_published_date("2026-09-05") == date(2026, 9, 5)
    assert detail_url_from_text(
        "onclick=go('/rule/detail/26/2981?from=list')", "https://school.xiaohongshu.com/rule/list/17",
    ) == "https://school.xiaohongshu.com/rule/detail/26/2981"
    metadata = extract_article_metadata(fixture_body("xhs_rule_article_old.html"))
    assert metadata["announced_at"] == "2026-08-01"
    assert metadata["effective_at"] == "2026-08-08"
    assert metadata["document_url"].endswith("e3_demo_old")
    assert metadata["external_links"] == [metadata["document_url"]]
    assert extract_external_links("https://a.test/x https://a.test/x https://b.test/y。") == [
        "https://a.test/x", "https://b.test/y",
    ]
    assert extract_rule_text_window(fixture_body("xhs_rule_article_old.html")).startswith("本规则于")


def test_notification_ledger_and_last_successful_run_persist(tmp_path) -> None:
    database = Database(tmp_path / "watch.db")
    keys = [("26/2981", "2026-08-27"), ("26/3059", "2026-09-01")]
    assert database.unseen_xhs_rule_notifications(keys) == set(keys)
    database.mark_xhs_rule_notifications([keys[0]])
    assert database.unseen_xhs_rule_notifications(keys) == {keys[1]}
    assert database.get_xhs_rule_last_successful_run() is None
    database.save_xhs_rule_last_successful_run("2026-09-09")
    assert database.get_xhs_rule_last_successful_run() == "2026-09-09"
    database.close()


@pytest.mark.asyncio
async def test_cold_start_baselines_every_current_rule_without_push(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    write_config(config)
    database = Database(tmp_path / "watch.db")
    delivered: list[dict] = []

    async def notifier(alert):
        delivered.append(alert)
        return {"feishu": "ok"}

    rows = [rule("26/2981", "2026-08-27"), rule("26/3059", "2026-09-08")]
    watcher = XhsRuleWatcher(
        database, config, notifier=notifier, alerts_path=tmp_path / "alerts.json",
        today_provider=lambda: TODAY,
    )
    monkeypatch.setattr(watcher, "_scrape_rule_lists", lambda *_: async_value(scrape_result(rows)))
    result = await watcher.run()
    assert result["mode"] == "baseline"
    assert result["rules_seen"] == 2 and result["notified"] == 0
    assert database.unseen_xhs_rule_notifications([
        ("26/2981", "2026-08-27"), ("26/3059", "2026-09-08"),
    ]) == set()
    assert database.get_xhs_rule_last_successful_run() == "2026-09-09"
    assert delivered == []
    assert not (tmp_path / "alerts.json").exists()
    database.close()


@pytest.mark.asyncio
async def test_new_today_rule_pushes_even_when_ai_says_no_change_and_never_repeats(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    write_config(config)
    database = Database(tmp_path / "watch.db")
    database.save_xhs_rule_last_successful_run("2026-09-08")
    delivered: list[dict] = []
    calls: list[str] = []
    clock = [TODAY]

    async def notifier(alert):
        delivered.append(alert)
        return {"feishu": "ok"}

    async def refresh(_settings):
        calls.append("shop")
        return {"stale": False, "items": [{"item_id": "1", "title": "电子题库"}]}

    async def analyze(_rule, _shop):
        calls.append("impact")
        return {
            "verdict": "no_change", "summary": "无影响", "affected_items": [],
            "action_plan": [], "manual_checks": [], "deadline": "2026-09-16",
        }

    watcher = XhsRuleWatcher(
        database, config, notifier=notifier, alerts_path=tmp_path / "alerts.json",
        shop_refresher=refresh, impact_analyzer=analyze, today_provider=lambda: clock[0],
    )
    rows = [rule("26/4000", "2026-09-09")]
    monkeypatch.setattr(watcher, "_scrape_rule_lists", lambda *_: async_value(scrape_result(rows)))
    monkeypatch.setattr(watcher, "_scrape", lambda *_: async_value({
        "lists": [], "articles": [({"name": rows[0]["title"], "url": rows[0]["url"]}, fixture_body("xhs_rule_article_new.html"))],
    }))

    first = await watcher.run()
    clock[0] = date(2026, 9, 10)
    second = await watcher.run()
    assert first["notified"] == 1 and first["status"] == "ok"
    assert second["notified"] == 0 and second["pending"] == 0
    assert calls == ["shop", "impact"]
    assert len(delivered) == 1
    assert "【小红书新规则】" in delivered[0]["summary"]
    assert "不涉及你当前在售的商品类目" in delivered[0]["summary"]
    assert json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))["items"] == delivered
    database.close()


@pytest.mark.asyncio
async def test_same_rule_id_with_new_monthly_publication_date_pushes_once(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    write_config(config)
    database = Database(tmp_path / "watch.db")
    database.save_xhs_rule_last_successful_run("2026-09-08")
    database.mark_xhs_rule_notifications([("26/2981", "2026-08-27")])
    delivered: list[dict] = []

    async def notifier(alert):
        delivered.append(alert)
        return {"feishu": "ok"}

    async def refresh(_settings):
        return {"stale": False, "items": [{"item_id": "1", "title": "电子题库"}]}

    async def analyze(_rule, _shop):
        return {
            "verdict": "review", "summary": "需核对", "affected_items": [],
            "action_plan": ["检查类目"], "manual_checks": [], "deadline": "2026-09-16",
        }

    changed = rule("26/2981", "2026-09-09", "月度类目规则")
    watcher = XhsRuleWatcher(
        database, config, notifier=notifier, alerts_path=tmp_path / "alerts.json",
        shop_refresher=refresh, impact_analyzer=analyze, today_provider=lambda: TODAY,
    )
    monkeypatch.setattr(watcher, "_scrape_rule_lists", lambda *_: async_value(scrape_result([changed])))
    monkeypatch.setattr(watcher, "_scrape", lambda *_: async_value({
        "lists": [], "articles": [(changed, fixture_body("xhs_rule_article_new.html"))],
    }))
    assert (await watcher.run())["notified"] == 1
    assert (await watcher.run())["notified"] == 0
    assert len(delivered) == 1
    assert database.unseen_xhs_rule_notifications([("26/2981", "2026-09-09")]) == set()
    database.close()


@pytest.mark.asyncio
async def test_failed_day_is_caught_up_by_two_day_window(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    write_config(config)
    database = Database(tmp_path / "watch.db")
    database.save_xhs_rule_last_successful_run("2026-09-07")
    delivered: list[dict] = []

    async def notifier(alert):
        delivered.append(alert)
        return {"feishu": "ok"}

    async def refresh(_settings):
        return {"stale": False, "items": [{"item_id": "1", "title": "课程"}]}

    async def analyze(_rule, _shop):
        return {
            "verdict": "action_required", "summary": "需修改", "affected_items": [],
            "action_plan": ["立即核对"], "manual_checks": [], "deadline": "2026-09-10",
        }

    yesterday = rule("26/5000", "2026-09-08")
    watcher = XhsRuleWatcher(
        database, config, notifier=notifier, alerts_path=tmp_path / "alerts.json",
        shop_refresher=refresh, impact_analyzer=analyze, today_provider=lambda: TODAY,
    )
    monkeypatch.setattr(watcher, "_scrape_rule_lists", lambda *_: async_value(scrape_result([yesterday])))
    monkeypatch.setattr(watcher, "_scrape", lambda *_: async_value({
        "lists": [], "articles": [(yesterday, fixture_body("xhs_rule_article_new.html"))],
    }))
    result = await watcher.run()
    assert result["window_start"] == "2026-09-05"
    assert result["notified"] == 1
    assert len(delivered) == 1
    database.close()


@pytest.mark.asyncio
async def test_manual_analyze_defaults_to_stdout_only_and_push_is_explicit(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    write_config(config)
    database = Database(tmp_path / "watch.db")
    delivered: list[dict] = []
    calls: list[str] = []

    async def refresh(settings):
        calls.append("shop")
        return {"stale": False, "items": [{"item_id": "1", "title": "电子题库"}]}

    async def analyze(rule_data, shop):
        calls.append("impact")
        return {
            "verdict": "review", "summary": "建议核对", "affected_items": [],
            "action_plan": ["核对类目"], "manual_checks": [], "deadline": rule_data["effective_at"],
        }

    async def notifier(alert):
        delivered.append(alert)
        return {"feishu": "ok"}

    watcher = XhsRuleWatcher(
        database, config, notifier=notifier, alerts_path=tmp_path / "alerts.json",
        shop_refresher=refresh, impact_analyzer=analyze, today_provider=lambda: TODAY,
    )
    article = {"name": "月度类目规则", "url": "https://school.xiaohongshu.com/rule/detail/26/2981"}
    monkeypatch.setattr(watcher, "_scrape", lambda *_: async_value({
        "lists": [], "articles": [(article, fixture_body("xhs_rule_article_new.html"))],
    }))
    result = await watcher.analyze("26/2981")
    assert result["status"] == "analyzed" and result["push_requested"] is False
    assert delivered == [] and not (tmp_path / "alerts.json").exists()
    pushed = await watcher.analyze("26/2981", push=True)
    assert pushed["status"] == "pushed" and pushed["push_requested"] is True
    assert calls == ["shop", "impact", "shop", "impact"]
    assert "【手动触发】" in delivered[0]["summary"]
    database.close()


@pytest.mark.asyncio
async def test_cookie_failure_alerts_once_without_raising(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xhs.yaml"
    write_config(config)
    database = Database(tmp_path / "watch.db")
    delivered: list[dict] = []

    async def notifier(alert):
        delivered.append(alert)
        return {"feishu": "ok"}

    async def expired(*_args):
        raise XhsCookieInvalid("login verification failed", status="degraded")

    watcher = XhsRuleWatcher(database, config, notifier=notifier, alerts_path=tmp_path / "alerts.json")
    monkeypatch.setattr(watcher, "_scrape_rule_lists", expired)
    assert (await watcher.run())["status"] == "degraded"
    assert (await watcher.run())["status"] == "degraded"
    assert len(delivered) == 1
    assert "重新导出" in delivered[0]["summary"]
    database.close()


async def async_value(value):
    return value
