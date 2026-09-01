from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.store.database import Database
from app.pipeline.trends import rank_history


def export_json(database: Database, generated_at: str, sources: list[str], output_dir: str | Path = "public/data") -> None:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    statuses = database.latest_statuses(sources)
    status_by_source = {row["source"]: row for row in statuses}
    for source in sources:
        source_items = database.current_items(source) if status_by_source[source]["status"] == "ok" else []
        for item in source_items:
            item["rank_history"] = rank_history(database, item["source"], item["url"], generated_at)
        payload = {
            "generated_at": generated_at,
            "source": source,
            "status": status_by_source[source],
            "items": source_items,
        }
        (target / f"{source}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    available_sources = {source for source in sources if status_by_source[source]["status"] == "ok"}
    all_items = [item for item in database.current_items() if item["source"] in available_sources]
    for item in all_items:
        item["rank_history"] = rank_history(database, item["source"], item["url"], generated_at)
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
