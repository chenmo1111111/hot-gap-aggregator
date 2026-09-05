"""Build the Feishu-ready autumn-recruitment feed from the existing jobs feed.

The public jobs collector is the upstream source on S1.  This module gives it a
stable, explicit ``qiuzhao.json`` contract without inventing unavailable data
such as application deadlines.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _text(value: object) -> str:
    return str(value or "").strip()


def _first(*values: object) -> str:
    return next((text for value in values if (text := _text(value))), "")


def _keywords(value: object) -> str:
    if isinstance(value, list):
        return "、".join(_text(item) for item in value if _text(item))
    return _text(value)


def normalize_jobs_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert published jobs items into the documented qiuzhao JSON shape."""
    rows = payload.get("items") if isinstance(payload.get("items"), list) else []
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        company = _first(row.get("company_name"), row.get("company"), extra.get("company"))
        position = _first(row.get("position"), row.get("job"), row.get("title_zh"), row.get("title"))
        if not company or not position:
            continue
        url = _first(row.get("apply_url"), row.get("url"))
        items.append({
            "company_name": company,
            "company_type": _first(row.get("company_type"), extra.get("company_type")),
            "industry": _first(row.get("industry"), extra.get("industry"), _keywords(extra.get("keywords_hit"))),
            "position": position,
            "location": _first(row.get("location"), row.get("city"), extra.get("city")),
            "education": _first(row.get("education"), extra.get("education")),
            "cohort": _first(row.get("cohort"), row.get("graduation_year"), extra.get("cohort")),
            "deadline": _first(row.get("deadline"), row.get("application_deadline"), extra.get("deadline")),
            "written_test": row.get("written_test", extra.get("written_test")),
            "apply_url": url,
            "announcement_url": _first(row.get("announcement_url"), extra.get("announcement_url"), url),
            "notes": _first(row.get("notes"), row.get("summary_zh")),
            "upstream_source": "jobs",
        })
    upstream_status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    status = {
        **upstream_status,
        "source": "qiuzhao",
        "item_count": len(items),
        "upstream_source": "jobs",
    }
    return {
        "generated_at": payload.get("generated_at"),
        "source": "qiuzhao",
        "status": status,
        "items": items,
    }


def write_qiuzhao(data_dir: str | Path) -> dict[str, Any]:
    target = Path(data_dir)
    source_path = target / "jobs.json"
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{source_path} must contain a JSON object")
    output = normalize_jobs_payload(payload)
    destination = target / "qiuzhao.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return output


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Create qiuzhao.json from the published jobs feed")
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    arguments = parser.parse_args()
    output = write_qiuzhao(arguments.data_dir)
    print(json.dumps({"event": "qiuzhao_exported", "item_count": len(output["items"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
