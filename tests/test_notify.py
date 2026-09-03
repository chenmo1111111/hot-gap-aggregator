from datetime import date, timedelta

import pytest

from app.models import Item
from app import notify
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


def test_alert_exam_type_bypasses_province_filter(tmp_path) -> None:
    config = tmp_path / "watch.yaml"
    config.write_text("provinces: [广东]\nexam_types_alert: [选调生, 国考]\n", encoding="utf-8")
    database = Database(tmp_path / "push.db")
    item = Item(
        source="gongkao", rank=1, title="江苏定向选调公告", title_zh="江苏定向选调公告",
        url="https://example.test/selection", is_new=True,
        extra={"id": "selection-1", "sub": "announcement", "province": "江苏", "exam_type": "选调生"},
    )
    lines, _ = build_gongkao_events([item], database, today=date(2026, 9, 3), config_path=config)
    assert len(lines) == 1
    assert lines[0].startswith("【选调预警】")
    assert "https://example.test/selection" in lines[0]
    database.close()


def test_focus_city_bypasses_province_filter(tmp_path) -> None:
    config = tmp_path / "watch.yaml"
    config.write_text("provinces: [山东]\ncities_focus: [德州, 石家庄]\nexam_types_alert: []\n", encoding="utf-8")
    database = Database(tmp_path / "push.db")
    item = Item(
        source="gongkao", rank=1, title="石家庄市事业单位考试公告", title_zh="石家庄市事业单位考试公告",
        url="https://example.test/sjz", is_new=True,
        extra={"id": "sjz-1", "sub": "announcement", "province": "河北", "exam_type": "事业单位"},
    )
    lines, _ = build_gongkao_events([item], database, today=date(2026, 9, 3), config_path=config)
    assert len(lines) == 1
    assert "重点城市：石家庄" in lines[0]
    database.close()


@pytest.mark.asyncio
async def test_subsidy_alert_prefers_feishu_card(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def post(url: str, **kwargs: object) -> None:
        calls.append((url, kwargs["json"]))

    monkeypatch.setenv("FEISHU_WEBHOOK", "https://feishu.test/hook")
    monkeypatch.setenv("BARK_URL", "https://bark.test/push")
    monkeypatch.setattr(notify, "_post", post)
    status = await notify.notify_subsidy_alert({
        "region": "沈阳市", "title": "生活补贴申领公告", "url": "https://example.test/notice",
        "type": "公告", "date": "2026-09-03", "created_at": "2026-09-03T00:00:00Z", "message": "预警",
    })
    assert status == {"feishu": "ok"}
    assert len(calls) == 1
    assert calls[0][1]["msg_type"] == "interactive"
