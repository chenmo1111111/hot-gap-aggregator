import json
import asyncio
from datetime import datetime, timezone
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
async def test_gongkao_fetches_hot_and_chronological_pages_with_bounded_concurrency(
    monkeypatch, tmp_path,
) -> None:
    collector = GongkaoCollector(tmp_path / "missing.yaml", tmp_path / "missing-fallback.yaml")
    calls: list[tuple[str, int]] = []
    active = 0
    max_active = 0

    class Response:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            return self.payload

    async def request(url: str, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        params = kwargs["params"]
        try:
            if url == collector.article_endpoint:
                offset = int(params["offset"])
                calls.append(("article", offset))
                article_id = 100 if offset == 50 else offset + 100
                return Response({"data": {"articles": [{
                    "id": article_id, "title": f"事业单位公开招聘公告{offset}",
                    "announcementArticleInfoRet": {"recruitNumRet": "1"},
                }]}})
            if url == collector.chronological_endpoint:
                calls.append(("recent", int(params["offset"])))
                if params["province"] == 2416 and params["exam"] == 4002:
                    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                    return Response({"data": {"articles": [{
                        "id": 999, "title": "内蒙古事业单位公开招聘公告",
                        "issueTime": now_ms,
                        "announcementArticleInfoRet": {"recruitNumRet": "1"},
                    }]}})
                return Response({"data": {"articles": []}})
            raise AssertionError("the Fenbi timeline endpoint must not be requested")
        finally:
            active -= 1

    async def post(_url: str, **_kwargs):
        return Response({"data": {"articles": []}})

    monkeypatch.setattr(collector, "request", request)
    monkeypatch.setattr(collector, "post", post)
    items = await collector.fetch()

    assert [offset for kind, offset in calls if kind == "article"] == [0, 50, 100, 150]
    assert [(item.extra["sub"], item.extra["id"]) for item in items] == [
        ("announcement", 100), ("announcement", 200), ("announcement", 250),
        ("announcement", 999),
    ]
    assert max_active <= 5


def test_chronological_article_uses_issue_time_and_structured_headcount() -> None:
    item = GongkaoCollector.parse_articles({"data": {"articles": [{
        "id": 99, "title": "事业单位公开招聘53人公告",
        "issueTime": 1_788_825_600_000, "updateTime": 1_788_912_000_000,
        "announcementArticleInfoRet": {"recruitNumRet": "53", "positionNum": 41},
        "tagsList": [{"type": 2, "name": "事业单位"}],
    }]}})[0]
    assert item.published_at.startswith("2026-09-08")
    assert item.extra["recruit_count"] == "53"
    assert item.extra["position_count"] == 41


def test_fallback_list_extracts_recent_title_date_link_and_province() -> None:
    html = """
    <ul><li><a href='/notice/1.html'>2027年度内蒙古自治区事业单位公开招聘公告</a>
    <span>2026-09-09</span></li><li><a href='/old'>旧招聘公告</a><span>2026-01-01</span></li></ul>
    """
    items = GongkaoCollector.parse_fallback_html(
        html, base_url="https://sydw.example/", source_site="huatu",
        today=datetime(2026, 9, 9, tzinfo=timezone.utc).date(),
    )
    assert len(items) == 1
    assert items[0].url == "https://sydw.example/notice/1.html"
    assert items[0].published_at == "2026-09-09"
    assert items[0].extra["province"] == "内蒙古"
    assert items[0].extra["source_site"] == "huatu"
