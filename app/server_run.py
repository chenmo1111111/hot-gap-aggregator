from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from app.collectors.scs import SCSCollector
from app.models import Item
from app.notify import build_gongkao_events, notify_priority_alert
from app.store.database import Database
from app.watchers.city_subsidy import CitySubsidyWatcher


LOGGER = logging.getLogger("hot-gap-server")
UTC = timezone.utc


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def merge_scs_into_site(data_dir: str | Path, items: list[Item], generated_at: str) -> None:
    target = Path(data_dir)
    all_path, gongkao_path = target / "all.json", target / "gongkao.json"
    if not all_path.exists() or not gongkao_path.exists():
        raise FileNotFoundError(f"deployed JSON is missing under {target}")
    all_payload = json.loads(all_path.read_text(encoding="utf-8"))
    gongkao_payload = json.loads(gongkao_path.read_text(encoding="utf-8"))
    official = [item.to_dict() for item in items]

    previous_gongkao = [
        item for item in gongkao_payload.get("items", [])
        if item.get("extra", {}).get("subsource") != "scs"
    ]
    combined = official + previous_gongkao
    for rank, item in enumerate(combined, 1):
        item["rank"] = rank
    gongkao_payload.update({"generated_at": generated_at, "items": combined})
    if isinstance(gongkao_payload.get("status"), dict):
        gongkao_payload["status"]["item_count"] = len(combined)
        gongkao_payload["status"]["server_scs"] = "ok"

    other_items = [
        item for item in all_payload.get("items", [])
        if item.get("extra", {}).get("subsource") != "scs"
    ]
    all_payload["items"] = other_items + official
    all_payload["server_generated_at"] = generated_at
    for status in all_payload.get("sources", []):
        if status.get("source") == "gongkao":
            status["item_count"] = len(combined)
            status["server_scs"] = "ok"
            break
    _write_json(gongkao_path, gongkao_payload)
    _write_json(all_path, all_payload)


async def run_scs(database: Database, data_dir: str | Path) -> dict[str, object]:
    started = time.perf_counter()
    collector = SCSCollector()
    try:
        items = await collector.fetch()
        run_at = datetime.now(UTC).isoformat()
        database.save_source(run_at, "gongkao", items, int((time.perf_counter() - started) * 1000))
        merge_scs_into_site(data_dir, items, run_at)
        lines, keys = build_gongkao_events(items, database)
        providers: dict[str, str] = {}
        if lines:
            providers = await notify_priority_alert("\n".join(lines), "国考与选调提醒")
            if any(status == "ok" for status in providers.values()):
                database.mark_gongkao_events(keys)
        return {"status": "ok", "item_count": len(items), "notifications": providers}
    except Exception as exc:
        LOGGER.warning("SCS server collector degraded: %s", exc)
        return {"status": "degraded", "error": str(exc)}


async def main(run_scs_job: bool = False, run_city_job: bool = False) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database = Database(os.getenv("SERVER_DATABASE", "data/server.db"))
    output: dict[str, object] = {}
    try:
        if run_scs_job:
            output["scs"] = await run_scs(database, os.getenv("SERVER_SITE_DATA_DIR", "/var/www/hot-gap/data"))
        if run_city_job:
            output["city_subsidy"] = await CitySubsidyWatcher(database).run()
        LOGGER.info(json.dumps({"event": "server_jobs_finished", **output}, ensure_ascii=False))
    finally:
        database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run mainland-only hot-gap jobs")
    parser.add_argument("--scs", action="store_true", help="collect the official national civil-service notices")
    parser.add_argument("--city", action="store_true", help="check configured city subsidy pages")
    arguments = parser.parse_args()
    asyncio.run(main(run_scs_job=arguments.scs, run_city_job=arguments.city))
