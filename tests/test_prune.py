import json
from datetime import UTC, date, datetime, timedelta

from app.models import Item
from app.pipeline.prune import (
    RetentionPolicy, is_expired_item, is_expired_public_gongkao, prune_database,
)
from app.store.database import Database, item_hash


POLICY = RetentionPolicy(vacuum_min_size_mb=9999)


def row(source: str, *, extra: dict | None = None, **values):
    return {"source": source, "extra": extra or {}, **values}


def test_job_deadline_boundary_keeps_today_and_deletes_yesterday() -> None:
    today = date(2026, 9, 8)
    assert not is_expired_item(row("jobs", deadline="2026-09-08"), POLICY, today=today)
    assert is_expired_item(row("jobs", deadline="2026-09-07"), POLICY, today=today)


def test_xjh_event_plus_one_day_boundary() -> None:
    event = row("jobs", published_at="2026-09-06", extra={"subsource": "xjh"})
    assert not is_expired_item(event, POLICY, today=date(2026, 9, 7))
    assert is_expired_item(event, POLICY, today=date(2026, 9, 8))


def test_gongkao_written_plus_seven_and_signup_plus_twenty_one_boundaries() -> None:
    written = row("gongkao", extra={"startWriteTime": "2026-09-01"})
    signup = row("gongkao", extra={"endSignUpTime": "2026-09-01"})
    assert not is_expired_item(written, POLICY, today=date(2026, 9, 8))
    assert is_expired_item(written, POLICY, today=date(2026, 9, 9))
    assert not is_expired_item(signup, POLICY, today=date(2026, 9, 22))
    assert is_expired_item(signup, POLICY, today=date(2026, 9, 23))


def test_public_gongkao_deadline_plus_three_and_written_plus_seven() -> None:
    signup = row("gongkao", extra={"endSignUpTime": "2026-09-05"})
    written = row("gongkao", extra={"startWriteTime": "2026-09-01"})
    assert not is_expired_public_gongkao(signup, POLICY, today=date(2026, 9, 8))
    assert is_expired_public_gongkao(signup, POLICY, today=date(2026, 9, 9))
    assert not is_expired_public_gongkao(written, POLICY, today=date(2026, 9, 8))
    assert is_expired_public_gongkao(written, POLICY, today=date(2026, 9, 9))


def test_public_gongkao_deadline_wins_over_written_date() -> None:
    both = row("gongkao", extra={
        "endSignUpTime": "2026-09-09", "startWriteTime": "2026-08-01",
    })
    assert not is_expired_public_gongkao(both, POLICY, today=date(2026, 9, 9))


def test_prune_database_removes_expired_rows_old_history_and_idle_caches(tmp_path) -> None:
    database = Database(tmp_path / "hot.db")
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    expired = Item(
        source="jobs", rank=1, title="过期岗位", title_zh="过期岗位",
        url="https://jobs.test/old", extra={"deadline": "2026-09-07"},
    )
    current = Item(
        source="jobs", rank=2, title="今日截止", title_zh="今日截止",
        url="https://jobs.test/today", extra={"deadline": "2026-09-08"},
    )
    database.save_source(now.isoformat(), "jobs", [expired, current], 1)
    old_run = (now - timedelta(days=5)).isoformat()
    latest_run = now.isoformat()
    database.connection.execute(
        "INSERT INTO snapshots(run_at,source,item_hash,rank,payload) VALUES(?,?,?,?,?)",
        (old_run, "weibo", "dropped", 2, "{}"),
    )
    database.connection.execute(
        "INSERT INTO snapshots(run_at,source,item_hash,rank,payload) VALUES(?,?,?,?,?)",
        (old_run, "weibo", "still-current", 1, "{}"),
    )
    database.connection.execute(
        "INSERT INTO snapshots(run_at,source,item_hash,rank,payload) VALUES(?,?,?,?,?)",
        (latest_run, "weibo", "still-current", 1, "{}"),
    )
    old = (now - timedelta(days=91)).isoformat()
    database.connection.execute(
        "INSERT INTO translations(text_hash,provider,source_text,translated_text,created_at,last_hit_at) VALUES(?,?,?,?,?,?)",
        ("old", "zhipu", "old", "旧", old, old),
    )
    database.connection.execute(
        "INSERT INTO summaries(text_hash,provider,source_text,summary_text,created_at,last_hit_at) VALUES(?,?,?,?,?,?)",
        ("old", "zhipu", "old", "旧", old, old),
    )
    database.connection.commit()

    stats = prune_database(database, POLICY, now=now)

    assert stats["jobs"] == 1
    assert stats["hot"] == 1
    assert [item["url"] for item in database.current_items("jobs")] == ["https://jobs.test/today"]
    assert database.connection.execute(
        "SELECT COUNT(*) FROM snapshots WHERE source='jobs' AND item_hash=?", (item_hash(expired),),
    ).fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0
    assert database.connection.execute(
        "SELECT COUNT(*) FROM snapshots WHERE source='weibo' AND item_hash='still-current'",
    ).fetchone()[0] == 2
    database.close()
