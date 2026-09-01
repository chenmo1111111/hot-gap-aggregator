from pathlib import Path

from app.collectors.telegram import TelegramCollector


def test_telegram_fixture() -> None:
    html = (Path(__file__).parent / "fixtures" / "telegram.html").read_text(encoding="utf-8")
    items = TelegramCollector.parse_channel(html, "example", translate=True)
    assert len(items) == 2
    assert items[0].published_at == "2026-08-31T09:00:00+00:00"
    assert items[0].extra == {"channel": "example", "translate": True}
    assert items[1].hot_value == "8.1K"
