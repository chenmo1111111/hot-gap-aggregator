from pathlib import Path

from app.collectors.xiaohongshu import XiaohongshuCollector, parse_like_count


def test_xiaohongshu_fixture_sorted_by_likes() -> None:
    html = (Path(__file__).parent / "fixtures" / "xiaohongshu.html").read_text(encoding="utf-8")
    items = XiaohongshuCollector.parse_html(html, "人工智能")
    assert [item.title for item in items] == ["AI 工具实测", "机器人创业观察"]
    assert items[0].extra["keyword"] == "人工智能"
    assert items[0].extra["status"] == "keyword-radar"
    assert items[1].thumbnail == "https://sns-img.example/b.jpg"


def test_xiaohongshu_like_count() -> None:
    assert parse_like_count("1.2万") == 12000
    assert parse_like_count("3.4K") == 3400
