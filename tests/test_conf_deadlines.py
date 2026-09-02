from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.collectors.conf_deadlines import ConfDeadlinesCollector


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 3, tzinfo=UTC)


def test_ai_deadlines_fixture_filters_watch_and_recent_deadlines(tmp_path) -> None:
    collector = ConfDeadlinesCollector(tmp_path / "unused.yaml", now=NOW)
    items = collector.parse_ai_deadlines(
        (FIXTURES / "conferences.yaml").read_text(encoding="utf-8"),
        ["NeurIPS", "ISMB/ECCB"],
    )
    assert [item.extra["conference"] for item in items] == ["NeurIPS", "ISMB/ECCB"]
    assert items[0].hot_value == "距截止 9 天"
    assert items[1].hot_value == "已截止 3 天"
    assert items[0].extra["subsource"] == "ai-deadlines"


def test_wikicfp_fixture_extracts_submission_deadline(tmp_path) -> None:
    collector = ConfDeadlinesCollector(tmp_path / "unused.yaml", now=NOW)
    items = collector.parse_wikicfp((FIXTURES / "wikicfp.xml").read_text(encoding="utf-8"), ["RECOMB"])
    assert len(items) == 1
    assert items[0].title == "RECOMB 2027 截止"
    assert items[0].extra["days_left"] == 17
    assert items[0].url.endswith("recomb-2027")


def test_ccfddl_nested_conference_schema(tmp_path) -> None:
    collector = ConfDeadlinesCollector(tmp_path / "unused.yaml", now=NOW)
    items = collector.parse_ai_deadlines((FIXTURES / "conferences.yaml").read_text(encoding="utf-8"), ["KDD"])
    assert len(items) == 1
    assert items[0].title == "KDD 2027 截止"
    assert items[0].extra["location"] == "Toronto, Canada"
    assert items[0].extra["days_left"] == 14


@pytest.mark.asyncio
async def test_conference_fetch_keeps_yaml_when_rss_fails(monkeypatch, tmp_path) -> None:
    config = tmp_path / "conferences.yaml"
    config.write_text("watch: [NeurIPS]\nai_deadlines_urls: [https://yaml.test/data]\nwikicfp_feeds: [https://rss.test/feed]\n", encoding="utf-8")
    collector = ConfDeadlinesCollector(config, now=NOW)

    class Response:
        text = (FIXTURES / "conferences.yaml").read_text(encoding="utf-8")

    async def request(url: str, **_kwargs):
        if "rss.test" in url:
            raise RuntimeError("RSSHub unavailable")
        return Response()

    monkeypatch.setattr(collector, "request", request)
    items = await collector.fetch()
    assert len(items) == 1
    assert items[0].rank == 1
