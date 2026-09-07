from pathlib import Path

from app.collectors.yingjiesheng import parse_search_html, parse_xjh_html


FIXTURES = Path(__file__).parent / "fixtures"


def test_yingjiesheng_search_mapping_and_filters() -> None:
    items = parse_search_html(
        (FIXTURES / "yingjiesheng_search.html").read_text(encoding="utf-8"),
        "算法", ["北京"], ["校园招聘"], 20,
    )
    assert len(items) == 1
    assert items[0].title == "算法工程师"
    assert items[0].extra == {
        "subsource": "yingjiesheng", "company": "示例科技", "city": "北京",
        "keywords_hit": ["算法"], "recruitment_type": "校园招聘", "is_central_soe": False,
    }
    assert items[0].published_at == "2026-09-06"


def test_yingjiesheng_xjh_mapping_and_city_filter() -> None:
    items = parse_xjh_html(
        (FIXTURES / "yingjiesheng_xjh.html").read_text(encoding="utf-8"), ["北京"], 20,
    )
    assert len(items) == 1
    assert items[0].title == "示例生物 宣讲会"
    assert items[0].extra["school"] == "清华大学"
    assert items[0].url == "https://my.yingjiesheng.com/xjh-001.html"
