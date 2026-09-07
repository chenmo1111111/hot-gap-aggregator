from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from app.notify import notify_priority_alert
from app.store.database import Database


LOGGER = logging.getLogger("hot-gap-heartbeat")
UTC = timezone.utc
ALERT_TEXT = "公考聚合主采集停了,GitHub Actions 每日兜底仍在"


def heartbeat_path() -> Path:
    data_dir = Path(os.getenv("SERVER_SITE_DATA_DIR", "/var/www/hot-gap/data"))
    return Path(os.getenv("SERVER_HEARTBEAT_PATH") or data_dir / "server-heartbeat.txt")


def read_heartbeat(path: Path) -> datetime | None:
    try:
        value = datetime.fromisoformat(path.read_text(encoding="utf-8").strip().replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def check_heartbeat(
    path: Path, database: Database, *, now: datetime | None = None,
    max_age: timedelta = timedelta(hours=4),
    notifier: Callable[[str, str], Awaitable[dict[str, str]]] = notify_priority_alert,
) -> dict[str, object]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    updated_at = read_heartbeat(path)
    if updated_at is not None and current - updated_at <= max_age:
        return {"status": "ok", "updated_at": updated_at.isoformat()}

    identity = updated_at.isoformat() if updated_at else "missing"
    event_key = "server-heartbeat-stale:" + hashlib.sha256(identity.encode()).hexdigest()[:20]
    if event_key not in database.unseen_push_events([event_key]):
        return {"status": "already_alerted", "updated_at": identity}
    providers = await notifier(ALERT_TEXT, "服务器采集告警")
    if any(status == "ok" for status in providers.values()):
        database.mark_push_events([event_key])
    return {"status": "stale", "updated_at": identity, "notifications": providers}


async def main(max_age_hours: float = 4.0) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database = Database(os.getenv("SERVER_DATABASE", "data/server.db"))
    try:
        result = await check_heartbeat(
            heartbeat_path(), database, max_age=timedelta(hours=max_age_hours),
        )
        LOGGER.info(json.dumps({"event": "server_heartbeat_checked", **result}, ensure_ascii=False))
    finally:
        database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Alert when the mainland main collector heartbeat is stale")
    parser.add_argument("--max-age-hours", type=float, default=4.0)
    args = parser.parse_args()
    asyncio.run(main(args.max_age_hours))
