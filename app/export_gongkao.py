"""Merge the published Gongkao feed with a captured Feishu Sheet snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from app.pipeline.gongkao_filter import filter_gongkao_items
from app.pipeline.gongkao_dedup import deduplicate_gongkao_items
from app.pipeline.prune import filter_current_items, load_retention


def _text(value: object) -> str:
    return str(value or "").strip()


def _extra(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("extra")
    return value if isinstance(value, Mapping) else {}


def _canonical_url(value: object) -> str:
    text = _text(value)
    if not text.startswith(("http://", "https://")):
        return ""
    parsed = urlsplit(text)
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(("", parsed.netloc.casefold(), path, parsed.query, ""))


def _items(payload: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    value = payload.get("items")
    if not isinstance(value, list):
        raise ValueError(f"{name} must contain an items list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{name} items[{index}] must be an object")
        result.append(dict(item))
    return result


def merge_gongkao_payloads(
    base_payload: Mapping[str, Any],
    sheet_payload: Mapping[str, Any] | None,
    server_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge CI, mainland watcher, and captured Sheet records without duplicates."""
    base_items = _items(base_payload, "gongkao.json")
    sheet_items = _items(sheet_payload, "gongkao_sheet.json") if sheet_payload else []
    server_items = _items(server_payload, "server-gongkao.json") if server_payload else []

    candidates: list[dict[str, Any]] = []

    def add_candidate(item: Mapping[str, Any], pool: str) -> None:
        candidate = dict(item)
        extra = dict(_extra(candidate))
        extra["dedup_pool"] = pool
        candidate["extra"] = extra
        candidates.append(candidate)

    for item in base_items:
        add_candidate(item, _text(_extra(item).get("dedup_pool")) or "base")
    for item in server_items:
        extra = dict(_extra(item))
        url = _canonical_url(item.get("url") or extra.get("announcement_url"))
        if not extra.get("id") and url:
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            extra["id"] = f"watcher:{digest}"
        extra.setdefault("sub", "announcement")
        item["extra"] = extra
        add_candidate(item, "server")

    for item in sheet_items:
        add_candidate(item, "sheet")

    merged, dedup_report = deduplicate_gongkao_items(candidates)

    def pool_counts(item: Mapping[str, Any]) -> dict[str, int]:
        extra = _extra(item)
        value = extra.get("merged_pool_counts")
        if isinstance(value, Mapping):
            return {str(name): int(count) for name, count in value.items()}
        return {_text(extra.get("dedup_pool")) or "base": 1}

    groups = [pool_counts(item) for item in merged]
    pool_duplicate_counts = {
        pool: sum(
            int(counts.get(pool, 0))
            - int(sum(int(value or 0) for value in counts.values()) == int(counts.get(pool, 0)))
            for counts in groups
            if counts.get(pool)
        )
        for pool in ("base", "server", "sheet", "xiaozhaoya", "purchased")
    }
    sheet_added_count = sum(
        counts.get("sheet", 0) > 0 and not counts.get("base") and not counts.get("server")
        for counts in groups
    )
    server_added_count = sum(
        counts.get("server", 0) > 0 and not counts.get("base")
        for counts in groups
    )
    sheet_duplicate_count = sum(
        counts.get("sheet", 0) - int(not counts.get("base") and not counts.get("server"))
        for counts in groups if counts.get("sheet")
    )
    server_duplicate_count = sum(
        counts.get("server", 0) - int(not counts.get("base"))
        for counts in groups if counts.get("server")
    )
    sheet_merged_count = sheet_duplicate_count
    server_merged_count = server_duplicate_count

    filter_input_count = len(merged)
    merged, filter_stats, filtered_samples = filter_gongkao_items(merged, profile="site")
    suspect_item_count = sum(bool(_extra(item).get("dup_suspect")) for item in merged)
    for index, item in enumerate(merged, 1):
        item["rank"] = index

    base_status = base_payload.get("status")
    status = dict(base_status) if isinstance(base_status, Mapping) else {}
    status.update(
        {
            "source": "gongkao",
            "item_count": len(merged),
            "base_item_count": len(base_items),
            "sheet_item_count": len(sheet_items),
            "server_item_count": len(server_items),
            "server_added_count": server_added_count,
            "server_duplicate_count": server_duplicate_count,
            "server_merged_count": server_merged_count,
            "sheet_added_count": sheet_added_count,
            "sheet_duplicate_count": sheet_duplicate_count,
            "sheet_merged_count": sheet_merged_count,
            "upstream_sources": [
                "gongkao",
                *(["gongkao_official"] if server_items else []),
                *(["feishu_sheet"] if sheet_items else []),
            ],
            "filter_input_count": filter_input_count,
            "filter_stats": filter_stats,
            "filtered_samples": filtered_samples,
            "dedup": dedup_report.to_dict(),
            "dedup_pool_duplicate_counts": pool_duplicate_counts,
            "dedup_suspect_item_count": suspect_item_count,
        }
    )
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": "gongkao",
        "status": status,
        "items": merged,
    }


def write_gongkao(data_dir: str | Path) -> dict[str, Any]:
    target = Path(data_dir)
    base_path = target / "gongkao.json"
    sheet_path = target / "gongkao_sheet.json"
    server_path = target / "server-gongkao.json"
    base_payload = json.loads(base_path.read_text(encoding="utf-8"))
    if not isinstance(base_payload, dict):
        raise ValueError("gongkao.json must contain a JSON object")
    xiaozhaoya_path = target / "xiaozhaoya_gongkao.json"
    xiaozhaoya_count = 0
    xiaozhaoya_input_count = 0
    xiaozhaoya_retention_deleted_count = 0
    xiaozhaoya_duplicate_count = 0
    if xiaozhaoya_path.exists():
        value = json.loads(xiaozhaoya_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("xiaozhaoya_gongkao.json must contain a JSON object")
        xiaozhaoya_items = _items(value, "xiaozhaoya_gongkao.json")
        xiaozhaoya_input_count = len(xiaozhaoya_items)
        xiaozhaoya_items = [
            {**item, "source": _text(item.get("source")) or "gongkao"}
            for item in xiaozhaoya_items
        ]
        xiaozhaoya_items, xiaozhaoya_retention_deleted_count = filter_current_items(
            xiaozhaoya_items, load_retention()
        )
        xiaozhaoya_count = len(xiaozhaoya_items)
        xiaozhaoya_items = [
            {**item, "extra": {**dict(_extra(item)), "dedup_pool": "xiaozhaoya"}}
            for item in xiaozhaoya_items
        ]
        base_items = _items(base_payload, "gongkao.json")
        base_payload = {
            **base_payload,
            "items": [*base_items, *xiaozhaoya_items],
        }
    purchased_path = target / "purchased_gongkao.json"
    purchased_input_count = 0
    purchased_count = 0
    purchased_retention_deleted_count = 0
    purchased_duplicate_count = 0
    if purchased_path.exists():
        value = json.loads(purchased_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("purchased_gongkao.json must contain a JSON object")
        purchased_items = _items(value, "purchased_gongkao.json")
        purchased_input_count = len(purchased_items)
        purchased_items, purchased_retention_deleted_count = filter_current_items(
            purchased_items, load_retention()
        )
        purchased_count = len(purchased_items)
        purchased_items = [
            {**item, "extra": {**dict(_extra(item)), "dedup_pool": "purchased"}}
            for item in purchased_items
        ]
        base_items = _items(base_payload, "gongkao.json")
        base_payload = {**base_payload, "items": [*base_items, *purchased_items]}
    sheet_payload: dict[str, Any] | None = None
    sheet_input_count = 0
    sheet_retention_deleted_count = 0
    if sheet_path.exists():
        value = json.loads(sheet_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("gongkao_sheet.json must contain a JSON object")
        sheet_items = _items(value, "gongkao_sheet.json")
        sheet_input_count = len(sheet_items)
        sheet_items, sheet_retention_deleted_count = filter_current_items(
            sheet_items, load_retention()
        )
        sheet_payload = {**value, "items": sheet_items}

    server_payload: dict[str, Any] | None = None
    if server_path.exists():
        value = json.loads(server_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("server-gongkao.json must contain a JSON object")
        server_payload = value

    output = merge_gongkao_payloads(base_payload, sheet_payload, server_payload)
    dedup_by_pool = output["status"].get("dedup_pool_duplicate_counts", {})
    xiaozhaoya_duplicate_count = dedup_by_pool.get("xiaozhaoya", 0)
    purchased_duplicate_count = dedup_by_pool.get("purchased", 0)
    xiaozhaoya_count = max(0, xiaozhaoya_count - xiaozhaoya_duplicate_count)
    purchased_count = max(0, purchased_count - purchased_duplicate_count)
    output["status"]["xiaozhaoya_item_count"] = xiaozhaoya_count
    output["status"]["xiaozhaoya_input_count"] = xiaozhaoya_input_count
    output["status"]["xiaozhaoya_retention_deleted_count"] = (
        xiaozhaoya_retention_deleted_count
    )
    output["status"]["xiaozhaoya_duplicate_count"] = xiaozhaoya_duplicate_count
    output["status"]["purchased_input_count"] = purchased_input_count
    output["status"]["purchased_item_count"] = purchased_count
    output["status"]["purchased_retention_deleted_count"] = (
        purchased_retention_deleted_count
    )
    output["status"]["purchased_duplicate_count"] = purchased_duplicate_count
    output["status"]["sheet_input_count"] = sheet_input_count
    output["status"]["sheet_retention_deleted_count"] = sheet_retention_deleted_count
    if xiaozhaoya_count:
        output["status"]["upstream_sources"] = [
            *output["status"]["upstream_sources"], "xiaozhaoya",
        ]
    if purchased_count:
        output["status"]["upstream_sources"] = [
            *output["status"]["upstream_sources"], "purchased_routed",
        ]
    destination = target / "gongkao_feishu.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return output


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Merge gongkao.json with gongkao_sheet.json")
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    arguments = parser.parse_args()
    output = write_gongkao(arguments.data_dir)
    print(
        json.dumps(
            {
                "event": "gongkao_exported",
                "item_count": len(output["items"]),
                "sheet_added_count": output["status"]["sheet_added_count"],
                "sheet_merged_count": output["status"]["sheet_merged_count"],
                "server_added_count": output["status"]["server_added_count"],
                "server_merged_count": output["status"]["server_merged_count"],
                "xiaozhaoya_item_count": output["status"].get("xiaozhaoya_item_count", 0),
                "purchased_item_count": output["status"].get("purchased_item_count", 0),
                "dedup_auto_merged_count": output["status"].get("dedup", {}).get("auto_merged_count", 0),
                "dedup_suspect_pair_count": output["status"].get("dedup", {}).get("suspect_pair_count", 0),
                "dedup_suspect_item_count": output["status"].get("dedup_suspect_item_count", 0),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
