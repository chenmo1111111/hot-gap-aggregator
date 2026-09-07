from datetime import UTC, datetime, timedelta

import pytest

from app.check_server_heartbeat import ALERT_TEXT, check_heartbeat, read_heartbeat
from app.store.database import Database


def test_read_heartbeat_accepts_utc_and_naive_values(tmp_path) -> None:
    path = tmp_path / "heartbeat.txt"
    path.write_text("2026-09-07T04:00:00+00:00", encoding="utf-8")
    assert read_heartbeat(path) == datetime(2026, 9, 7, 4, tzinfo=UTC)
    path.write_text("2026-09-07T04:00:00", encoding="utf-8")
    assert read_heartbeat(path) == datetime(2026, 9, 7, 4, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fresh_heartbeat_does_not_notify(tmp_path) -> None:
    path = tmp_path / "heartbeat.txt"
    path.write_text("2026-09-07T04:00:00+00:00", encoding="utf-8")
    calls = []

    async def notifier(text, title):
        calls.append((text, title))
        return {"feishu": "ok"}

    database = Database(tmp_path / "server.db")
    result = await check_heartbeat(
        path, database, now=datetime(2026, 9, 7, 7, tzinfo=UTC), notifier=notifier,
    )
    database.close()
    assert result["status"] == "ok"
    assert calls == []


@pytest.mark.asyncio
async def test_stale_heartbeat_notifies_once_per_timestamp(tmp_path) -> None:
    path = tmp_path / "heartbeat.txt"
    path.write_text("2026-09-07T00:00:00+00:00", encoding="utf-8")
    calls = []

    async def notifier(text, title):
        calls.append((text, title))
        return {"feishu": "ok"}

    database = Database(tmp_path / "server.db")
    now = datetime(2026, 9, 7, 5, tzinfo=UTC)
    first = await check_heartbeat(path, database, now=now, max_age=timedelta(hours=4), notifier=notifier)
    second = await check_heartbeat(path, database, now=now, max_age=timedelta(hours=4), notifier=notifier)
    database.close()
    assert first["status"] == "stale"
    assert second["status"] == "already_alerted"
    assert calls == [(ALERT_TEXT, "服务器采集告警")]
