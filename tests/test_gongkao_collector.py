import json
from pathlib import Path

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
