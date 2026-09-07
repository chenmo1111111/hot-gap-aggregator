"""Merge the published Gongkao feed with a captured Feishu Sheet snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


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


def _identity_keys(item: Mapping[str, Any]) -> set[str]:
    extra = _extra(item)
    url = _canonical_url(
        item.get("url")
        or item.get("announcement_url")
        or extra.get("announcement_url")
        or extra.get("url")
    )
    title = _normalize(item.get("title_zh") or item.get("title"))
    province = _normalize(extra.get("province") or item.get("province"))
    keys = {f"url:{url}"} if url else set()
    if title:
        keys.add(f"title:{title}|{province}")
    return keys


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
) -> dict[str, Any]:
    """Return base records followed by only non-duplicate Sheet records."""
    base_items = _items(base_payload, "gongkao.json")
    sheet_items = _items(sheet_payload, "gongkao_sheet.json") if sheet_payload else []

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_count = 0
    # Existing Gongkao records are authoritative and retain their cardinality,
    # IDs and order even if the upstream feed itself contains similar entries.
    for item in base_items:
        seen.update(_identity_keys(item))
        item["rank"] = len(merged) + 1
        merged.append(item)
    for item in sheet_items:
        keys = _identity_keys(item)
        if keys and seen.intersection(keys):
            duplicate_count += 1
            continue
        seen.update(keys)
        item["rank"] = len(merged) + 1
        merged.append(item)

    base_status = base_payload.get("status")
    status = dict(base_status) if isinstance(base_status, Mapping) else {}
    status.update(
        {
            "source": "gongkao",
            "item_count": len(merged),
            "base_item_count": len(base_items),
            "sheet_item_count": len(sheet_items),
            "sheet_added_count": len(merged) - len(base_items),
            "sheet_duplicate_count": duplicate_count,
            "upstream_sources": ["gongkao", "feishu_sheet"] if sheet_items else ["gongkao"],
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
    base_payload = json.loads(base_path.read_text(encoding="utf-8"))
    if not isinstance(base_payload, dict):
        raise ValueError("gongkao.json must contain a JSON object")
    sheet_payload: dict[str, Any] | None = None
    if sheet_path.exists():
        value = json.loads(sheet_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("gongkao_sheet.json must contain a JSON object")
        sheet_payload = value

    output = merge_gongkao_payloads(base_payload, sheet_payload)
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
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
