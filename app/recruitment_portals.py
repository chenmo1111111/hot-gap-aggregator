"""Build the static official recruitment portal directory from checked YAML."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


EXPECTED_GROUPS = ("官方平台", "省人社厅", "央企")


def load_recruitment_portals(config_path: str | Path) -> list[dict[str, Any]]:
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    groups = raw.get("groups", []) if isinstance(raw, dict) else []
    if not isinstance(groups, list):
        raise ValueError("recruitment portal groups must be a list")
    output: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or group.get("name") not in EXPECTED_GROUPS:
            raise ValueError("unknown recruitment portal group")
        items: list[dict[str, str]] = []
        for row in group.get("items", []):
            if not isinstance(row, dict):
                continue
            name, url = str(row.get("name") or "").strip(), str(row.get("url") or "").strip()
            parsed = urlparse(url)
            if not name or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"invalid recruitment portal: {name or url}")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            item = {"name": name, "url": url, "category": str(row.get("category") or group["name"])}
            if row.get("province"):
                item["province"] = str(row["province"])
            items.append(item)
        output.append({"name": group["name"], "items": items})
    if tuple(group["name"] for group in output) != EXPECTED_GROUPS:
        raise ValueError(f"portal groups must be ordered as: {', '.join(EXPECTED_GROUPS)}")
    return output


def export_recruitment_portals(
    config_path: str | Path = "config/recruitment_portals.yaml",
    output_path: str | Path = "public/data/portals.json",
) -> dict[str, Any]:
    groups = load_recruitment_portals(config_path)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "item_count": sum(len(group["items"]) for group in groups),
        "groups": groups,
    }
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/recruitment_portals.yaml")
    parser.add_argument("--output", default="public/data/portals.json")
    args = parser.parse_args()
    payload = export_recruitment_portals(args.config, args.output)
    print(json.dumps({"event": "recruitment_portals_exported", "item_count": payload["item_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
