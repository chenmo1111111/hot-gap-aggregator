import json
from pathlib import Path

import pytest

from app.collectors.gongkao import GongkaoCollector
from app.collectors.gongkao_types import article_type, timeline_type

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_gongkao_articles_fixture() -> None:
    item = GongkaoCollector.parse_articles(load("gongkao_articles.json"))[0]
    assert item.title_zh == item.title
    assert item.hot_value == "报名中"
    assert item.extra["id"] == 101
    assert item.extra["sub"] == "announcement"
    assert item.extra["record_kind"] in {"公考", "秋招"}
    assert item.extra["tags"] == ["国考", "遴选"]
    assert item.extra["province"] == "全国"
    assert item.extra["exam_type"] == "国考"


def test_gongkao_timeline_fixture() -> None:
    item = GongkaoCollector.parse_timeline(load("gongkao_timeline.json"))[0]
    assert item.extra["sub"] == "timeline"
    assert "报名" in (item.summary_zh or "")
    assert item.url.endswith("/202")


def test_selection_and_national_exam_types_are_normalized() -> None:
    assert timeline_type(3) == "选调生"
    assert timeline_type(99, "某省定向选调公告") == "选调生"
    assert timeline_type(1) == "国考"
    assert article_type([], "中央机关及其直属机构考试录用公务员公告") == "国考"
    assert article_type([{"type": 2, "name": "中央选调"}]) == "选调生"


@pytest.mark.asyncio
async def test_gongkao_fetches_four_article_pages_and_deduplicates(monkeypatch, tmp_path) -> None:
    collector = GongkaoCollector(tmp_path / "missing.yaml")
    calls: list[tuple[str, int]] = []

    class Response:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            return self.payload

    async def request(url: str, **kwargs):
        params = kwargs["params"]
        if url == collector.article_endpoint:
            offset = int(params["offset"])
            calls.append(("article", offset))
            article_id = 100 if offset == 50 else offset + 100
            return Response({"data": {"articles": [{"id": article_id, "title": f"公告{offset}"}]}})
        calls.append(("timeline", int(params["offset"])))
        return Response({"datas": [{"id": 100, "topic": "考试日历"}]})

    monkeypatch.setattr(collector, "request", request)
    items = await collector.fetch()

    assert [offset for kind, offset in calls if kind == "article"] == [0, 50, 100, 150]
    # The duplicate article ID from offset 50 is removed, but a timeline with
    # the same numeric ID remains because it is a different record kind.
    assert [(item.extra["sub"], item.extra["id"]) for item in items] == [
        ("announcement", 100), ("announcement", 200), ("announcement", 250),
        ("timeline", 100),
    ]
