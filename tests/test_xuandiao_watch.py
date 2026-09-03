import json
from pathlib import Path

import pytest

from app.store.database import Database
from app.watchers.xuandiao_watch import XuandiaoWatcher, parse_hebei_search, parse_hlj_api


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_official_json_parsers_map_notice_urls_and_dates() -> None:
    hlj = parse_hlj_api(json.loads(fixture("xuandiao_hlj.json")))[0]
    assert hlj["title"] == "黑龙江省2027年选调应届优秀高校毕业生公告"
    assert hlj["url"].endswith("newsDetails.html?id=hlj-1&type=1")
    assert hlj["date"] == "2026-09-03"
    hebei = parse_hebei_search(json.loads(fixture("xuandiao_hebei.json")))
    assert hebei[0]["url"] == "https://www.hebpta.com.cn/article?id=13108"
    assert hebei[1]["url"] == "https://official.test/shared"


async def value(text: str) -> str:
    return text


@pytest.mark.asyncio
async def test_xuandiao_baseline_then_school_alert_and_gongkao_items(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xuandiao.yaml"
    config.write_text(
        "list_pages:\n  - {region: 辽宁, url: 'https://official.test/list/'}\n"
        "title_keywords: [选调, 急需紧缺]\n"
        "my_universities: [东北林业大学, 东北林大, NEFU]\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "watch.db")
    alerts = tmp_path / "alerts.json"
    watcher = XuandiaoWatcher(database, config, alerts_path=alerts)
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    monkeypatch.delenv("BARK_URL", raising=False)
    monkeypatch.setattr(watcher, "_fetch_response", lambda *_args, **_kwargs: value(fixture("xuandiao_old.html")))
    first = await watcher.run()
    assert first["list_pages"][0]["status"] == "baseline"
    assert watcher.latest_items[0].source == "gongkao"
    assert watcher.latest_items[0].extra["subsource"] == "xuandiao"
    assert watcher.latest_items[0].extra["exam_type"] == "选调生"
    assert not alerts.exists()

    monkeypatch.setattr(watcher, "_fetch_response", lambda *_args, **_kwargs: value(fixture("xuandiao_new.html")))
    second = await watcher.run()
    assert second["list_pages"][0]["status"] == "pushed"
    assert watcher.latest_items[0].extra["target_university_hit"] == ["东北林业大学"]
    payload = json.loads(alerts.read_text(encoding="utf-8"))
    assert payload["items"][0]["priority"] == "highest"
    assert payload["items"][0]["category_label"] == "选调预警"
    assert "你的学校" in payload["items"][0]["message"]
    assert (await watcher.run())["list_pages"][0]["status"] == "unchanged"
    database.close()


@pytest.mark.asyncio
async def test_xuandiao_one_failed_province_does_not_block_other(monkeypatch, tmp_path) -> None:
    config = tmp_path / "xuandiao.yaml"
    config.write_text(
        "list_pages:\n  - {region: 黑龙江, url: 'https://bad.test/'}\n"
        "  - {region: 辽宁, url: 'https://ok.test/'}\n"
        "title_keywords: [选调]\nmy_universities: []\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "watch.db")
    watcher = XuandiaoWatcher(database, config, alerts_path=tmp_path / "alerts.json")

    async def fetch(url: str, **_kwargs):
        if "bad" in url:
            raise RuntimeError("blocked")
        return fixture("xuandiao_old.html")

    monkeypatch.setattr(watcher, "_fetch_response", fetch)
    result = await watcher.run()
    assert [row["status"] for row in result["list_pages"]] == ["degraded", "baseline"]
    assert len(watcher.latest_items) == 1
    database.close()
