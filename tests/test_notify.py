from datetime import date, timedelta

from app.models import Item
from app.notify import build_gongkao_events, build_top20
from app.store.database import Database


def test_top20_prioritizes_cross_source_clusters() -> None:
    ordinary = Item(source="weibo", rank=1, title="普通热点", title_zh="普通热点", url="https://example.com/1")
    clustered = Item(source="douyin", rank=20, title="共同热点", title_zh="共同热点", url="https://example.com/2", cluster_size=3)
    digest = build_top20([ordinary, clustered])
    assert digest.splitlines()[1].endswith("共同热点")


def test_gongkao_node_events_are_filtered_and_deduplicated(tmp_path) -> None:
    config = tmp_path / "watch.yaml"
    config.write_text("provinces:\n  - 广东\n", encoding="utf-8")
    database = Database(tmp_path / "push.db")
    today = date(2026, 8, 31)
    item = Item(
        source="gongkao", rank=1, title="广东省考公告", title_zh="广东省考公告", url="https://example.com/gd",
        is_new=True, extra={
            "id": 42, "sub": "announcement", "province": "广东",
            "startSignUpTime": (today + timedelta(days=1)).isoformat(),
            "endSignUpTime": (today + timedelta(days=2)).isoformat(),
            "startWriteTime": (today + timedelta(days=3)).isoformat(),
        },
    )
    lines, keys = build_gongkao_events([item], database, today=today, config_path=config)
    assert len(lines) == 4
    database.mark_gongkao_events(keys)
    assert build_gongkao_events([item], database, today=today, config_path=config) == ([], [])
    database.close()
