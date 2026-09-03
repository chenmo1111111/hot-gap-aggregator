from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import yaml

from app.store.database import Database
from app.pipeline.trends import rank_history


def export_json(database: Database, generated_at: str, sources: list[str], output_dir: str | Path = "public/data") -> None:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    statuses = database.latest_statuses(sources)
    status_by_source = {row["source"]: row for row in statuses}
    def with_history(items: list[dict]) -> list[dict]:
        output = deepcopy(items)
        for item in output:
            item["rank_history"] = rank_history(database, item["source"], item["url"], generated_at)
        return output

    items_by_source = {
        source: database.current_items(source) if status_by_source[source]["status"] == "ok" else []
        for source in sources
    }
    for source in sources:
        source_items = with_history(items_by_source[source])
        payload = {
            "generated_at": generated_at,
            "source": source,
            "status": status_by_source[source],
            "items": source_items,
        }
        (target / f"{source}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    feed_status = status_by_source.get("feed", {"source": "feed", "status": "not_run", "item_count": 0})
    feed_groups: dict[str, list[dict]] = {tab: [] for tab in ("hot", "ai", "papers", "tools", "jobs")}
    for item in items_by_source.get("feed", []):
        tab = str(item.get("extra", {}).get("tab") or "hot")
        if tab in feed_groups:
            feed_groups[tab].append(item)

    derived_statuses: list[dict] = []
    for tab in ("ai", "tools"):
        tab_status = {
            **feed_status, "source": tab, "item_count": len(feed_groups[tab]),
        }
        derived_statuses.append(tab_status)
        (target / f"{tab}.json").write_text(json.dumps({
            "generated_at": generated_at, "source": tab, "status": tab_status,
            "items": with_history(feed_groups[tab]),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    for tab in ("papers", "jobs"):
        native_status = status_by_source.get(tab, {"source": tab, "status": "not_run", "item_count": 0})
        merged = [*items_by_source.get(tab, []), *feed_groups[tab]]
        combined_status = {
            **native_status,
            "status": "ok" if native_status.get("status") == "ok" or feed_status.get("status") == "ok" else native_status.get("status", "not_run"),
            "item_count": len(merged),
        }
        status_by_source[tab] = combined_status
        (target / f"{tab}.json").write_text(json.dumps({
            "generated_at": generated_at, "source": tab, "status": combined_status,
            "items": with_history(merged),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    statuses = [status_by_source[row["source"]] for row in statuses]
    statuses.extend(derived_statuses)
    all_items_raw = [
        item for source in sources if source != "feed"
        for item in items_by_source[source]
    ]
    all_items = with_history([*all_items_raw, *feed_groups["hot"]])
    all_payload = {
        "generated_at": generated_at,
        "sources": statuses,
        "items": all_items,
    }
    (target / "all.json").write_text(json.dumps(all_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sites_path = Path("data/gongkao_official_sites.yaml")
    if sites_path.exists():
        sites = yaml.safe_load(sites_path.read_text(encoding="utf-8")) or []
        (target / "gongkao_official_sites.json").write_text(
            json.dumps({"verified": True, "sites": sites}, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    jobs_path = Path("config/job_radar.yaml")
    if jobs_path.exists():
        jobs_config = yaml.safe_load(jobs_path.read_text(encoding="utf-8")) or {}
        quicklinks = jobs_config.get("companies_quicklink", []) if isinstance(jobs_config, dict) else []
        (target / "job_quicklinks.json").write_text(
            json.dumps({"items": quicklinks}, ensure_ascii=False, indent=2), encoding="utf-8",
        )
