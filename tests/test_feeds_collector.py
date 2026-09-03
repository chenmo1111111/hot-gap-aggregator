from pathlib import Path

import pytest

from app.collectors.base import SourceUnavailable
from app.collectors.feeds import FeedsCollector
from app.pipeline.processor import process_items
from app.pipeline.translator import Translator
from app.store.database import Database
from app.store.exporter import export_json
from app.models import Item
import json

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parses_rss_atom_html_dates_and_tab_groups() -> None:
    zhihu = FeedsCollector.parse(fixture("feeds_zhihu.xml"), {
        "name": "知乎热榜", "route": "/zhihu/hotlist", "tab": "hot", "translate": False, "limit": 20,
    })
    nature = FeedsCollector.parse(fixture("feeds_nature.xml"), {
        "name": "Nature", "route": "/nature/research/nature", "tab": "papers", "translate": True, "limit": 15,
    })
    release = FeedsCollector.parse(fixture("feeds_github_release.xml"), {
        "name": "scanpy 发版", "route": "/github/release/scverse/scanpy", "tab": "tools", "translate": True, "limit": 5,
    })
    items = [*zhihu, *nature, *release]
    grouped = FeedsCollector.group_by_tab(items)
    assert zhihu[0].title_zh == zhihu[0].title
    assert zhihu[0].published_at == "2026-09-03T08:00:00+00:00"
    assert nature[0].extra["description"] == "A benchmark for finding rare cell populations."
    assert nature[0].extra["translate"] is True
    assert release[0].extra["description"] == "Highlights Faster neighbors and improved plotting."
    assert [len(grouped[tab]) for tab in ("hot", "papers", "tools")] == [1, 1, 1]


@pytest.mark.asyncio
async def test_fetch_survives_one_feed_failure_deduplicates_and_redacts_key(monkeypatch, tmp_path, caplog) -> None:
    config = tmp_path / "feeds.yaml"
    config.write_text("""
feeds:
  - {name: 知乎, route: /zhihu/hotlist, tab: hot, translate: false, limit: 20}
  - {name: Nature, route: /nature/research/nature, tab: papers, translate: true, limit: 15}
  - {name: 坏路由, route: /broken/secret, tab: ai, translate: false, limit: 10}
  - {name: 待配置, route: /x-mol/paper/0/<待填magazine_id>, tab: papers, translate: false, limit: 10}
""", encoding="utf-8")
    collector = FeedsCollector(config)

    class Response:
        def __init__(self, content: bytes) -> None:
            self.content = content

    async def request(url: str, **kwargs):
        assert kwargs["params"]["key"] == "fixture-secret"
        if "broken" in url:
            raise RuntimeError("request key=fixture-secret failed")
        return Response(fixture("feeds_nature.xml") if "nature" in url else fixture("feeds_zhihu.xml"))

    monkeypatch.setenv("RSSHUB_BASE", "https://rsshub.example.test")
    monkeypatch.setenv("RSSHUB_KEY", "fixture-secret")
    monkeypatch.setattr(collector, "request", request)
    items = await collector.fetch()
    assert len(items) == 2
    assert [item.rank for item in items] == [1, 2]
    assert "fixture-secret" not in caplog.text
    assert "[redacted]" in caplog.text


@pytest.mark.asyncio
async def test_missing_base_skips_without_network(monkeypatch, tmp_path) -> None:
    config = tmp_path / "feeds.yaml"
    config.write_text("feeds: []\n", encoding="utf-8")
    monkeypatch.delenv("RSSHUB_BASE", raising=False)
    with pytest.raises(SourceUnavailable, match="not configured") as captured:
        await FeedsCollector(config).fetch()
    assert captured.value.status == "skipped"


@pytest.mark.asyncio
async def test_all_routes_failing_degrades(monkeypatch, tmp_path) -> None:
    config = tmp_path / "feeds.yaml"
    config.write_text("feeds:\n  - {name: broken, route: /broken, tab: ai}\n", encoding="utf-8")
    collector = FeedsCollector(config)
    monkeypatch.setenv("RSSHUB_BASE", "https://rsshub.example.test")

    async def request(_url: str, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(collector, "request", request)
    with pytest.raises(SourceUnavailable, match="All RSSHub feeds failed") as captured:
        await collector.fetch()
    assert captured.value.status == "degraded"


class FeedTranslator(Translator):
    provider = "feed-test"

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return [f"中文：{text}" for text in texts]


@pytest.mark.asyncio
async def test_processor_translates_only_opted_in_feed(tmp_path) -> None:
    database = Database(tmp_path / "feed.db")
    translator = FeedTranslator(database)
    translated = FeedsCollector.parse(fixture("feeds_nature.xml"), {
        "name": "Nature", "route": "/nature", "tab": "papers", "translate": True,
    })[0]
    untranslated = FeedsCollector.parse(fixture("feeds_github_release.xml"), {
        "name": "scanpy", "route": "/github/release", "tab": "tools", "translate": False,
    })[0]
    await process_items([translated, untranslated], translator)
    assert translated.title_zh.startswith("中文：")
    assert translated.summary_zh and translated.summary_zh.startswith("中文：")
    assert untranslated.title_zh == untranslated.title
    assert untranslated.summary_zh == untranslated.extra["description"]
    database.close()


def test_export_splits_feed_tabs_and_merges_papers_jobs(tmp_path) -> None:
    database = Database(tmp_path / "export.db")
    run_at = "2026-09-03T08:00:00+00:00"
    feed_items = [
        Item(source="feed", rank=1, title="知乎", title_zh="知乎", url="https://example.test/hot", extra={"tab": "hot", "feed_name": "知乎"}),
        Item(source="feed", rank=2, title="AI", title_zh="AI", url="https://example.test/ai", extra={"tab": "ai", "feed_name": "量子位"}),
        Item(source="feed", rank=3, title="Paper", title_zh="论文", url="https://example.test/paper", extra={"tab": "papers", "feed_name": "Nature"}),
        Item(source="feed", rank=4, title="Tool", title_zh="工具", url="https://example.test/tool", extra={"tab": "tools", "feed_name": "scanpy"}),
        Item(source="feed", rank=5, title="Job", title_zh="岗位", url="https://example.test/job", extra={"tab": "jobs", "feed_name": "实习僧"}),
    ]
    native_paper = Item(source="papers", rank=1, title="Native", title_zh="原生论文", url="https://example.test/native-paper")
    native_job = Item(source="jobs", rank=1, title="Native job", title_zh="原生岗位", url="https://example.test/native-job")
    database.save_source(run_at, "feed", feed_items, 10)
    database.save_source(run_at, "papers", [native_paper], 10)
    database.save_source(run_at, "jobs", [native_job], 10)
    output = tmp_path / "public"
    export_json(database, run_at, ["papers", "jobs", "feed"], output)

    all_payload = json.loads((output / "all.json").read_text(encoding="utf-8"))
    ai_payload = json.loads((output / "ai.json").read_text(encoding="utf-8"))
    tools_payload = json.loads((output / "tools.json").read_text(encoding="utf-8"))
    papers_payload = json.loads((output / "papers.json").read_text(encoding="utf-8"))
    jobs_payload = json.loads((output / "jobs.json").read_text(encoding="utf-8"))
    assert {item["url"] for item in all_payload["items"]} == {
        "https://example.test/hot", "https://example.test/native-paper", "https://example.test/native-job",
    }
    assert [item["url"] for item in ai_payload["items"]] == ["https://example.test/ai"]
    assert [item["url"] for item in tools_payload["items"]] == ["https://example.test/tool"]
    assert {item["source"] for item in papers_payload["items"]} == {"papers", "feed"}
    assert {item["source"] for item in jobs_payload["items"]} == {"jobs", "feed"}
    assert {state["source"] for state in all_payload["sources"]} >= {"feed", "ai", "tools"}
    database.close()
