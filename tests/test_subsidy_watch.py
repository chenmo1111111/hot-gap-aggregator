import json
from pathlib import Path

import pytest

from app.store.database import Database
from app.watchers.subsidy_watch import SubsidyWatcher, parse_hebei_home_menu, parse_list_html


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_html_list_parser_extracts_absolute_url_and_date() -> None:
    rows = parse_list_html(fixture("subsidy_list_new.html"), "https://example.test/notices/")
    assert rows[0] == {
        "title": "青年人才购房补贴新一批申领公告",
        "url": "https://example.test/notice/3.html",
        "date": "2026-09-03",
    }


def test_hebei_official_api_parser_keeps_notice_tab_only() -> None:
    payload = json.loads(fixture("subsidy_hebei.json"))
    rows = parse_hebei_home_menu(payload, "https://rst.hebei.gov.cn/pageWarp?isId={id}&id=1")
    assert len(rows) == 1
    assert rows[0]["url"].endswith("isId=901&id=1")


@pytest.mark.asyncio
async def test_list_watcher_baselines_then_writes_one_fallback_alert(monkeypatch, tmp_path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "list_pages:\n  - region: 石家庄市\n    url: https://example.test/notices/\n"
        "title_keywords: [补贴, 毕业生]\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "watch.db")
    alerts = tmp_path / "alerts.json"
    watcher = SubsidyWatcher(database, config, alerts_path=alerts)
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    monkeypatch.delenv("BARK_URL", raising=False)
    monkeypatch.setattr(watcher, "_fetch_response", lambda _url: _async_value(fixture("subsidy_list_old.html")))
    first = await watcher.run()
    assert first["list_pages"][0]["status"] == "baseline"
    assert not alerts.exists()

    monkeypatch.setattr(watcher, "_fetch_response", lambda _url: _async_value(fixture("subsidy_list_new.html")))
    second = await watcher.run()
    assert second["list_pages"][0]["status"] == "pushed"
    payload = json.loads(alerts.read_text(encoding="utf-8"))
    assert [item["title"] for item in payload["items"]] == ["青年人才购房补贴新一批申领公告"]
    assert (await watcher.run())["list_pages"][0]["status"] == "unchanged"
    database.close()


@pytest.mark.asyncio
async def test_one_failed_source_does_not_block_the_rest(monkeypatch, tmp_path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "list_pages:\n"
        "  - {region: 失败市, url: 'https://bad.test/'}\n"
        "  - {region: 正常市, url: 'https://ok.test/'}\n"
        "title_keywords: [补贴]\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "watch.db")
    watcher = SubsidyWatcher(database, config, alerts_path=tmp_path / "alerts.json")

    async def fetch(url: str):
        if "bad" in url:
            raise RuntimeError("blocked")
        return fixture("subsidy_list_old.html")

    monkeypatch.setattr(watcher, "_fetch_response", fetch)
    results = await watcher.run()
    assert [row["status"] for row in results["list_pages"]] == ["degraded", "baseline"]
    database.close()


async def _async_value(value: str) -> str:
    return value
