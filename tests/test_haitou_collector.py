from pathlib import Path

from app.collectors.haitou import parse_haitou_html


FIXTURES = Path(__file__).parent / "fixtures"


def test_haitou_fixture_maps_campaign_and_deadline() -> None:
    items = parse_haitou_html(
        (FIXTURES / "haitou_campaigns.html").read_text(encoding="utf-8"),
        "算法", ["北京", "天津"], 20,
    )
    assert len(items) == 1
    assert items[0].url == "https://sx.haitou.cc/article/12345.html"
    assert items[0].extra["subsource"] == "haitou"
    assert items[0].extra["deadline"] == "2026-10-31"
    assert items[0].extra["city"] == "北京、天津"
