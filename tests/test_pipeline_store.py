import json

from app.models import Item
from app.pipeline.language import is_chinese
from app.store.database import Database
from app.store.exporter import export_json


def test_language_heuristic() -> None:
    assert is_chinese("今天的 AI 热点")
    assert not is_chinese("OpenAI launches a new model")


def test_translation_cache_and_export(tmp_path) -> None:
    database = Database(tmp_path / "hot.db")
    database.save_translations({"Hello world": "你好，世界"})
    assert database.get_translations(["Hello world"]) == {"Hello world": "你好，世界"}
    item = Item(source="weibo", rank=1, title="测试热搜", title_zh="测试热搜", url="https://example.com")
    run_at = "2026-08-31T02:00:00+00:00"
    database.save_source(run_at, "weibo", [item], 12)
    export_json(database, run_at, ["weibo"], tmp_path / "public")
    payload = json.loads((tmp_path / "public" / "all.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["is_new"] is True
    assert payload["sources"][0]["status"] == "ok"
    database.close()


def test_history_is_relative_to_previous_run_and_consecutive_days(tmp_path) -> None:
    database = Database(tmp_path / "history.db")
    first = Item(source="weibo", rank=5, title="同一热点", title_zh="同一热点", url="https://example.com/1")
    second = Item(source="weibo", rank=3, title="同一热点", title_zh="同一热点", url="https://example.com/1")
    database.save_source("2026-08-30T02:00:00+00:00", "weibo", [first], 10)
    database.save_source("2026-08-31T02:00:00+00:00", "weibo", [second], 10)
    current = database.current_items("weibo")[0]
    assert current["is_new"] is False
    assert current["rank_delta"] == 2
    assert current["days_on_board"] == 2
    database.close()
