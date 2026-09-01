import json
from pathlib import Path

import pytest

from app.collectors.bilibili import BilibiliCollector
from app.collectors.github import GitHubCollector
from app.collectors.weibo import WeiboCollector
from app.collectors.youtube import YouTubeCollector

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_weibo_collector_parses_fixture_and_skips_ads() -> None:
    items = WeiboCollector.parse(load_json("weibo.json"))
    assert [item.title for item in items] == ["开学第一课", "人工智能新进展"]
    assert items[0].hot_value == "1086421"
    assert "%23" in items[0].url


def test_bilibili_collector_parses_fixture() -> None:
    item = BilibiliCollector.parse(load_json("bilibili.json"))[0]
    assert item.title_zh == "我们把机器人送进了工厂"
    assert item.url == "https://b23.tv/demo"
    assert item.thumbnail.endswith("demo.jpg")
    assert item.published_at.startswith("2024-")


def test_github_collector_parses_fixture() -> None:
    items = GitHubCollector.parse((FIXTURES / "github.html").read_text(encoding="utf-8"))
    assert items[0].title == "openai/codex"
    assert items[0].hot_value == "1,284 stars today"
    assert items[0].extra["description"].startswith("A lightweight")


def test_youtube_collector_parses_fixture() -> None:
    item = YouTubeCollector.parse(load_json("youtube.json"), "US")[0]
    assert item.url == "https://youtube.com/watch?v=abc123"
    assert item.hot_value == "3829014"
    assert item.extra["region"] == "US"


def test_youtube_invidious_fixture() -> None:
    payload = load_json("youtube_invidious.json")
    item = YouTubeCollector.parse_invidious(payload)[0]
    assert item.url.endswith("inv123")
    assert item.extra["via"] == "invidious"
    assert item.thumbnail.endswith("large.jpg")


@pytest.mark.asyncio
async def test_youtube_invidious_rolls_to_next_instance(monkeypatch, tmp_path) -> None:
    config = tmp_path / "instances.yaml"
    config.write_text("- https://bad.example\n- https://good.example\n", encoding="utf-8")
    monkeypatch.setenv("INVIDIOUS_INSTANCES_CONFIG", str(config))
    monkeypatch.delenv("INVIDIOUS_BASE", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    collector = YouTubeCollector()
    calls: list[str] = []

    async def fake_fetch(base: str):
        calls.append(base)
        if "bad" in base:
            raise RuntimeError("blocked")
        return YouTubeCollector.parse_invidious(load_json("youtube_invidious.json"), "US")

    monkeypatch.setattr(collector, "_fetch_invidious_instance", fake_fetch)
    items = await collector.fetch()
    assert calls == ["https://bad.example", "https://good.example"]
    assert items[0].extra["via"] == "invidious"
