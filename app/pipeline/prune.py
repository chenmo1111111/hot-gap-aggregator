from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from app.store.database import Database


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")
DEFAULT_HOT_SOURCES = (
    "weibo", "bilibili", "douyin", "github", "telegram", "zhihu",
    "youtube", "ai", "tools",
)


@dataclass(frozen=True)
class RetentionPolicy:
    jobs_delete_after_deadline: bool = True
    xjh_delete_after_event_days: int = 1
    gongkao_write_plus_days: int = 7
    gongkao_signup_plus_days: int = 21
    gongkao_public_signup_plus_days: int = 3
    gongkao_public_written_plus_days: int = 7
    gongkao_public_no_date_days: int = 45
    hot_sources: tuple[str, ...] = DEFAULT_HOT_SOURCES
    hot_max_age_days: int = 4
    snapshots_max_age_days: int = 45
    cache_max_idle_days: int = 90
    vacuum_min_size_mb: int = 50


def load_retention(path: str | Path | None = None) -> RetentionPolicy:
    target = Path(path or os.getenv("RETENTION_CONFIG", "config/retention.yaml"))
    if not target.exists():
        return RetentionPolicy()
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    jobs = raw.get("jobs") if isinstance(raw.get("jobs"), Mapping) else {}
    gongkao = raw.get("gongkao") if isinstance(raw.get("gongkao"), Mapping) else {}
    hot_sources = tuple(str(value) for value in raw.get("hot_sources", []) if str(value))
    return RetentionPolicy(
        jobs_delete_after_deadline=bool(jobs.get("delete_after_deadline", True)),
        xjh_delete_after_event_days=max(0, int(jobs.get("xjh_delete_after_event_days", 1))),
        gongkao_write_plus_days=max(0, int(gongkao.get("keep_until_write_exam_plus_days", 7))),
        gongkao_signup_plus_days=max(0, int(gongkao.get("no_write_date_signup_plus_days", 21))),
        gongkao_public_signup_plus_days=max(
            0, int(gongkao.get("public_signup_grace_days", 3))
        ),
        gongkao_public_written_plus_days=max(
            0, int(gongkao.get("public_written_grace_days", 7))
        ),
        gongkao_public_no_date_days=max(
            1, int(gongkao.get("public_no_date_max_age_days", 45))
        ),
        hot_sources=hot_sources or DEFAULT_HOT_SOURCES,
        hot_max_age_days=max(1, int(raw.get("hot_max_age_days", 4))),
        snapshots_max_age_days=max(1, int(raw.get("snapshots_max_age_days", 45))),
        cache_max_idle_days=max(1, int(raw.get("cache_max_idle_days", 90))),
        vacuum_min_size_mb=max(1, int(raw.get("vacuum_min_size_mb", 50))),
    )


def _date_value(value: object) -> date | None:
    if value in (None, "", "/"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    match = DATE_RE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _extra(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("extra")
    return value if isinstance(value, Mapping) else {}


def _first_date(item: Mapping[str, Any], names: tuple[str, ...]) -> date | None:
    extra = _extra(item)
    for name in names:
        value = extra.get(name) if name.startswith("extra.") else item.get(name)
        if name.startswith("extra."):
            value = extra.get(name.removeprefix("extra."))
        parsed = _date_value(value)
        if parsed:
            return parsed
    return None


def is_expired_item(
    item: Mapping[str, Any], policy: RetentionPolicy, *, today: date | None = None,
) -> bool:
    current = today or datetime.now(UTC).date()
    source = str(item.get("source") or "").casefold()
    extra = _extra(item)
    if source == "jobs":
        subsource = str(extra.get("subsource") or "").casefold()
        if subsource == "xjh":
            event = _first_date(item, (
                "extra.event_date", "extra.event_time", "extra.start_time",
                "extra.date", "event_date", "published_at",
            ))
            return bool(event and current > event + timedelta(days=policy.xjh_delete_after_event_days))
        if not policy.jobs_delete_after_deadline:
            return False
        deadline = _first_date(item, (
            "extra.deadline", "extra.application_deadline", "deadline",
            "application_deadline", "end_time", "extra.end_time",
        ))
        return bool(deadline and deadline < current)
    if source == "gongkao":
        written = _first_date(item, (
            "extra.startWriteTime", "extra.written_exam", "startWriteTime",
            "written_exam", "笔试时间",
        ))
        if written:
            return current > written + timedelta(days=policy.gongkao_write_plus_days)
        signup_end = _first_date(item, (
            "extra.endSignUpTime", "extra.signup_end", "endSignUpTime",
            "signup_end", "报名截止", "截止日期",
        ))
        return bool(signup_end and current > signup_end + timedelta(days=policy.gongkao_signup_plus_days))
    return False


def filter_current_items(
    rows: list[dict[str, Any]], policy: RetentionPolicy, *, today: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    kept = [row for row in rows if not is_expired_item(row, policy, today=today)]
    for rank, row in enumerate(kept, 1):
        row["rank"] = rank
    return kept, len(rows) - len(kept)


def is_expired_public_gongkao(
    item: Mapping[str, Any], policy: RetentionPolicy, *, today: date | None = None,
) -> bool:
    """Apply the shorter retention window used by the sold/public table.

    A known signup deadline wins.  Only when it is absent do we fall back to
    the written-exam date, matching the product rule exactly.
    """
    current = today or datetime.now(UTC).date()
    signup_end = _first_date(item, (
        "extra.endSignUpTime", "extra.signup_end", "endSignUpTime",
        "signup_end", "报名截止", "截止日期",
    ))
    if signup_end:
        return current > signup_end + timedelta(days=policy.gongkao_public_signup_plus_days)
    written = _first_date(item, (
        "extra.startWriteTime", "extra.written_exam", "startWriteTime",
        "written_exam", "笔试时间",
    ))
    if written:
        return current > written + timedelta(days=policy.gongkao_public_written_plus_days)
    published = _first_date(item, (
        "published_at", "extra.issueTime", "extra.first_seen", "首次收录",
    ))
    return bool(
        published and current > published + timedelta(days=policy.gongkao_public_no_date_days)
    )


def filter_current_public_gongkao(
    rows: list[dict[str, Any]], policy: RetentionPolicy, *, today: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    kept = [row for row in rows if not is_expired_public_gongkao(row, policy, today=today)]
    for rank, row in enumerate(kept, 1):
        row["rank"] = rank
    return kept, len(rows) - len(kept)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


def _delete_expired_current(
    connection: sqlite3.Connection, source: str, policy: RetentionPolicy, today: date,
) -> int:
    rows = connection.execute(
        "SELECT item_hash,payload FROM items WHERE source=?", (source,),
    ).fetchall()
    hashes: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, Mapping) and is_expired_item(payload, policy, today=today):
            hashes.append(str(row["item_hash"]))
    if not hashes:
        return 0
    placeholders = ",".join("?" for _ in hashes)
    connection.execute(
        f"DELETE FROM items WHERE source=? AND item_hash IN ({placeholders})", [source, *hashes],
    )
    connection.execute(
        f"DELETE FROM snapshots WHERE source=? AND item_hash IN ({placeholders})", [source, *hashes],
    )
    return len(hashes)


def prune_database(
    database: Database, policy: RetentionPolicy | None = None,
    *, now: datetime | None = None,
) -> dict[str, int | bool]:
    policy = policy or load_retention()
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    today = moment.date()
    connection = database.connection
    with connection:
        jobs_deleted = _delete_expired_current(connection, "jobs", policy, today)
        gongkao_deleted = _delete_expired_current(connection, "gongkao", policy, today)

        hot_deleted = 0
        hot_cutoff = (moment - timedelta(days=policy.hot_max_age_days)).isoformat()
        # RSS items are stored under the shared ``feed`` source even when the
        # exported tabs are ai/tools/zhihu-like hot feeds.
        database_sources = tuple(dict.fromkeys([*policy.hot_sources, "feed"]))
        for source in database_sources:
            latest = connection.execute(
                "SELECT MAX(run_at) AS run_at FROM snapshots WHERE source=?", (source,),
            ).fetchone()["run_at"]
            if not latest:
                continue
            current_hashes = {
                str(row["item_hash"])
                for row in connection.execute(
                    "SELECT item_hash FROM snapshots WHERE source=? AND run_at=?", (source, latest),
                )
            }
            if current_hashes:
                placeholders = ",".join("?" for _ in current_hashes)
                hot_deleted += connection.execute(
                    f"DELETE FROM snapshots WHERE source=? AND run_at<? AND item_hash NOT IN ({placeholders})",
                    [source, hot_cutoff, *current_hashes],
                ).rowcount

        snapshot_cutoff = (moment - timedelta(days=policy.snapshots_max_age_days)).isoformat()
        snapshots_deleted = connection.execute(
            "DELETE FROM snapshots WHERE run_at<?", (snapshot_cutoff,),
        ).rowcount
        source_runs_deleted = connection.execute(
            "DELETE FROM source_runs WHERE run_at<?", (snapshot_cutoff,),
        ).rowcount

        cache_cutoff = (moment - timedelta(days=policy.cache_max_idle_days)).isoformat()
        translations_deleted = connection.execute(
            "DELETE FROM translations WHERE COALESCE(last_hit_at,created_at)<?", (cache_cutoff,),
        ).rowcount
        summaries_deleted = connection.execute(
            "DELETE FROM summaries WHERE COALESCE(last_hit_at,created_at)<?", (cache_cutoff,),
        ).rowcount
        enrichment_deleted = 0
        if _table_exists(connection, "gongkao_enrichment"):
            enrichment_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(gongkao_enrichment)")
            }
            timestamp_column = "last_hit_at" if "last_hit_at" in enrichment_columns else "提取时间"
            enrichment_deleted = connection.execute(
                f'DELETE FROM gongkao_enrichment WHERE COALESCE("{timestamp_column}","提取时间")<?',
                (cache_cutoff,),
            ).rowcount

    LOGGER.info(
        "prune: jobs 删%d gongkao 删%d hot 删%d", jobs_deleted, gongkao_deleted, hot_deleted,
    )
    vacuumed = False
    try:
        threshold = policy.vacuum_min_size_mb * 1024 * 1024
        if database.path.exists() and database.path.stat().st_size > threshold:
            connection.execute("VACUUM")
            connection.execute("PRAGMA optimize")
            vacuumed = True
    except (OSError, sqlite3.DatabaseError) as exc:
        LOGGER.warning("database vacuum skipped: %s", exc)
    return {
        "jobs": jobs_deleted, "gongkao": gongkao_deleted, "hot": hot_deleted,
        "snapshots": snapshots_deleted, "source_runs": source_runs_deleted,
        "translations": translations_deleted, "summaries": summaries_deleted,
        "gongkao_enrichment": enrichment_deleted, "vacuumed": vacuumed,
    }
