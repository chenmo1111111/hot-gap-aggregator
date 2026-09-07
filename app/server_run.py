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
from app.watchers.campus_jobs import CampusJobsWatcher
from app.watchers.subsidy_watch import SubsidyWatcher
from app.watchers.xuandiao_watch import XuandiaoWatcher
from app.watchers.xhs_rule_watch import XhsRuleWatcher


LOGGER = logging.getLogger("hot-gap-server")
UTC = timezone.utc
SERVER_GONGKAO_FILENAME = "server-gongkao.json"
SERVER_JOBS_FILENAME = "server-jobs.json"


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_server_gongkao(target: Path) -> dict:
    """Load the server-owned sidecar without ever taking ownership of CI output."""
    sidecar_path = target / SERVER_GONGKAO_FILENAME
    if sidecar_path.exists():
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Ignoring invalid %s; a fresh sidecar will be written", sidecar_path)

    # One-time compatibility migration for servers that used the old in-place
    # merger. This reads official rows only; it never rewrites all.json or
    # gongkao.json, which remain exclusively owned by the GitHub deployment.
    legacy_items: list[dict] = []
    gongkao_path = target / "gongkao.json"
    if gongkao_path.exists():
        try:
            deployed = json.loads(gongkao_path.read_text(encoding="utf-8"))
            legacy_items = [
                row for row in deployed.get("items", [])
                if isinstance(row, dict)
                and row.get("extra", {}).get("subsource") in {"scs", "xuandiao", "campus"}
            ]
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Could not inspect legacy official gongkao rows under %s", target)
    return {"generated_at": "", "source": "gongkao_official", "subsources": {}, "items": legacy_items}


def _merge_server_gongkao(
    data_dir: str | Path,
    items: list[Item],
    generated_at: str,
    subsource: str,
    preserve_regions: set[str] | None = None,
) -> None:
    target = Path(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    payload = _load_server_gongkao(target)
    incoming = [item.to_dict() for item in items]
    preserved: list[dict] = []
    for row in payload.get("items", []):
        if not isinstance(row, dict):
            continue
        row_subsource = row.get("extra", {}).get("subsource")
        if row_subsource != subsource:
            preserved.append(row)
            continue
        if preserve_regions and str(row.get("extra", {}).get("province") or "") in preserve_regions:
            preserved.append(row)

    combined: list[dict] = []
    seen_urls: set[str] = set()
    for row in incoming + preserved:
        url = str(row.get("url") or "").strip().rstrip("/").casefold()
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        combined.append(row)
    for rank, row in enumerate(combined, 1):
        row["rank"] = rank

    subsources = payload.get("subsources") if isinstance(payload.get("subsources"), dict) else {}
    subsources[subsource] = {
        "status": "ok",
        "item_count": sum(
            1 for row in combined if row.get("extra", {}).get("subsource") == subsource
        ),
        "updated_at": generated_at,
    }
    output = {
        "generated_at": generated_at,
        "source": "gongkao_official",
        "status": {
            "source": "gongkao_official", "status": "ok", "item_count": len(combined),
        },
        "subsources": subsources,
        "items": combined,
    }
    _write_json(target / SERVER_GONGKAO_FILENAME, output)


def merge_scs_into_site(data_dir: str | Path, items: list[Item], generated_at: str) -> None:
    _merge_server_gongkao(data_dir, items, generated_at, "scs")


def merge_xuandiao_into_site(
    data_dir: str | Path, items: list[Item], generated_at: str, preserve_regions: set[str] | None = None,
) -> None:
    _merge_server_gongkao(data_dir, items, generated_at, "xuandiao", preserve_regions)


def _load_server_jobs(target: Path) -> dict:
    path = target / SERVER_JOBS_FILENAME
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Ignoring invalid %s; a fresh sidecar will be written", path)
    return {"generated_at": "", "source": "jobs_official", "subsources": {}, "items": []}


def merge_campus_jobs_into_site(
    data_dir: str | Path, items: list[Item], generated_at: str,
    preserve_schools: set[str] | None = None,
) -> None:
    target = Path(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    payload = _load_server_jobs(target)
    preserved: list[dict] = []
    for row in payload.get("items", []):
        if not isinstance(row, dict):
            continue
        extra = row.get("extra", {}) if isinstance(row.get("extra"), dict) else {}
        if extra.get("subsource") != "campus" or (
            preserve_schools and str(extra.get("school") or "") in preserve_schools
        ):
            preserved.append(row)
    seen: set[str] = set()
    combined: list[dict] = []
    for row in [*(item.to_dict() for item in items), *preserved]:
        url = str(row.get("url") or "").strip().rstrip("/").casefold()
        if not url or url in seen:
            continue
        seen.add(url)
        row["rank"] = len(combined) + 1
        combined.append(row)
    subsources = payload.get("subsources") if isinstance(payload.get("subsources"), dict) else {}
    subsources["campus"] = {
        "status": "ok", "item_count": sum(1 for row in combined if row.get("extra", {}).get("subsource") == "campus"),
        "updated_at": generated_at,
    }
    _write_json(target / SERVER_JOBS_FILENAME, {
        "generated_at": generated_at, "source": "jobs_official",
        "status": {"source": "jobs_official", "status": "ok", "item_count": len(combined)},
        "subsources": subsources, "items": combined,
    })


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


async def run_xuandiao(database: Database, data_dir: str | Path) -> dict[str, object]:
    watcher = XuandiaoWatcher(database)
    try:
        result = await watcher.run()
        reports = result.get("list_pages", [])
        degraded_regions = {str(row.get("region") or "") for row in reports if row.get("status") == "degraded"}
        if reports and len(degraded_regions) == len(reports):
            return {"status": "degraded", "item_count": 0, **result}
        run_at = datetime.now(UTC).isoformat()
        merge_xuandiao_into_site(data_dir, watcher.latest_items, run_at, degraded_regions)
        return {"status": "ok", "item_count": len(watcher.latest_items), **result}
    except Exception as exc:
        LOGGER.warning("xuandiao watcher degraded: %s", exc)
        return {"status": "degraded", "error": str(exc)}


async def run_xhs_rules(database: Database) -> dict[str, object]:
    try:
        return await XhsRuleWatcher(database).run()
    except Exception as exc:
        # This job is deliberately isolated from SCS/subsidy/xuandiao. A page
        # redesign or bad local config must never stop another mainland job.
        LOGGER.warning("XHS rule watcher degraded: %s", exc)
        return {"status": "degraded", "error": str(exc)}


async def run_campus_jobs(database: Database, data_dir: str | Path) -> dict[str, object]:
    watcher = CampusJobsWatcher(database)
    try:
        result = await watcher.run()
        reports = result.get("list_pages", [])
        degraded_schools = {str(row.get("school") or "") for row in reports if row.get("status") == "degraded"}
        if reports and len(degraded_schools) == len(reports):
            return {"status": "degraded", "item_count": 0, **result}
        run_at = datetime.now(UTC).isoformat()
        merge_campus_jobs_into_site(data_dir, watcher.latest_items, run_at, degraded_schools)
        _merge_server_gongkao(data_dir, watcher.latest_gongkao_items, run_at, "campus")
        return {
            "status": "ok", "item_count": len(watcher.latest_items),
            "gongkao_item_count": len(watcher.latest_gongkao_items), **result,
        }
    except Exception as exc:
        LOGGER.warning("campus jobs watcher degraded: %s", exc)
        return {"status": "degraded", "error": str(exc)}


async def main(
    run_scs_job: bool = False, run_subsidy_job: bool = False,
    run_xuandiao_job: bool = False, run_xhs_rules_job: bool = False,
    run_campus_jobs_job: bool = False,
) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database = Database(os.getenv("SERVER_DATABASE", "data/server.db"))
    output: dict[str, object] = {}
    try:
        if run_scs_job:
            output["scs"] = await run_scs(database, os.getenv("SERVER_SITE_DATA_DIR", "/var/www/hot-gap/data"))
        if run_subsidy_job:
            output["subsidy_watch"] = await SubsidyWatcher(database).run()
        if run_xuandiao_job:
            output["xuandiao_watch"] = await run_xuandiao(database, os.getenv("SERVER_SITE_DATA_DIR", "/var/www/hot-gap/data"))
        if run_xhs_rules_job:
            output["xhs_rule_watch"] = await run_xhs_rules(database)
        if run_campus_jobs_job:
            output["campus_jobs"] = await run_campus_jobs(database, os.getenv("SERVER_SITE_DATA_DIR", "/var/www/hot-gap/data"))
        LOGGER.info(json.dumps({"event": "server_jobs_finished", **output}, ensure_ascii=False))
    finally:
        database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run mainland-only hot-gap jobs")
    parser.add_argument("--scs", action="store_true", help="collect the official national civil-service notices")
    parser.add_argument("--subsidy", action="store_true", help="check HRSS notice lists and core subsidy policy pages")
    parser.add_argument("--city", action="store_true", help="deprecated alias for --subsidy")
    parser.add_argument("--xuandiao", action="store_true", help="check official selection-graduate notice lists")
    parser.add_argument("--xhs-rules", action="store_true", help="check Xiaohongshu e-commerce rule changes")
    parser.add_argument("--campus-jobs", action="store_true", help="check mainland university recruitment lists")
    arguments = parser.parse_args()
    asyncio.run(main(
        run_scs_job=arguments.scs,
        run_subsidy_job=arguments.subsidy or arguments.city,
        run_xuandiao_job=arguments.xuandiao,
        run_xhs_rules_job=arguments.xhs_rules,
        run_campus_jobs_job=arguments.campus_jobs,
    ))
