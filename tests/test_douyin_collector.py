import json
from pathlib import Path

from app.collectors.douyin import DouyinCollector


def test_douyin_fixture() -> None:
    payload = json.loads((Path(__file__).parent / "fixtures" / "douyin.json").read_text(encoding="utf-8"))
    items = DouyinCollector.parse(payload)
    assert items[0].rank == 1
    assert items[0].hot_value == "9876543"
    assert "%E6%9C%BA%E5%99%A8%E4%BA%BA" in items[0].url
