from app.models import Item
from app.pipeline.trends import derive_trends
from app.store.database import Database


def item(title: str, rank: int) -> Item:
    return Item(source="weibo", rank=rank, title=title, title_zh=title, url=f"https://example.com/{title}")


def test_trends_rising_new_dropped_and_longest(tmp_path) -> None:
    database = Database(tmp_path / "trends.db")
    database.save_source("2026-08-30T09:00:00+00:00", "weibo", [item("A", 5), item("B", 2)], 10)
    database.save_source("2026-08-31T09:00:00+00:00", "weibo", [item("A", 1), item("C", 3)], 10)
    trends = derive_trends(database, "2026-08-31T09:00:00+00:00")
    assert trends["rising"][0]["title"] == "A"
    assert trends["rising"][0]["rank_delta"] == 4
    assert [row["title"] for row in trends["new_today"]] == ["C"]
    assert [row["title"] for row in trends["dropped"]] == ["B"]
    assert trends["longest_on_board"][0]["title"] == "A"
    assert len(trends["rising"][0]["rank_history"]) == 2
    database.close()
