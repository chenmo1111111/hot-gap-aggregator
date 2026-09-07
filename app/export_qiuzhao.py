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


SOURCE_LABELS = {
    "tencent": "大厂雷达·腾讯", "bytedance": "大厂雷达·字节",
    "yingjiesheng": "应届生", "xjh": "应届生宣讲会", "haitou": "海投校招",
    "wutongguo": "梧桐果校招", "guopin": "国聘",
    "campus": "高校就业网",
}


def _source_label(extra: dict[str, Any], row: dict[str, Any]) -> str:
    explicit = _first(row.get("source_label"), row.get("source_name"), extra.get("source_label"))
    if explicit:
        return explicit
    subsource = _text(extra.get("subsource"))
    return SOURCE_LABELS.get(subsource, subsource)


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
        item = {
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
        }
        if label := _source_label(extra, row):
            item["source_label"] = label
        items.append(item)
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


def normalize_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and preserve a manually captured standardized Qiuzhao snapshot."""
    rows = payload.get("items")
    if not isinstance(rows, list):
        raise ValueError("qiuzhao_wanqing.json must contain an items list")

    items: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"qiuzhao_wanqing.json items[{index}] must be an object")
        company = _text(row.get("company_name"))
        position = _text(row.get("position"))
        if not company or not position:
            raise ValueError(
                f"qiuzhao_wanqing.json items[{index}] needs company_name and position"
            )
        items.append(dict(row))

    source_status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    return {
        "generated_at": payload.get("generated_at"),
        "source": "qiuzhao",
        "status": {
            **source_status,
            "source": "qiuzhao",
            "item_count": len(items),
            "upstream_source": "wanqing_feishu",
        },
        "items": items,
    }


def write_qiuzhao(data_dir: str | Path) -> dict[str, Any]:
    target = Path(data_dir)
    snapshot_path = target / "qiuzhao_wanqing.json"
    inputs: list[dict[str, Any]] = []
    if snapshot_path.exists():
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{snapshot_path} must contain a JSON object")
        inputs.append(normalize_snapshot_payload(payload))
    for filename in ("jobs.json", "server-jobs.json"):
        source_path = target / filename
        if not source_path.exists():
            continue
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{source_path} must contain a JSON object")
        inputs.append(normalize_jobs_payload(payload))
    if not inputs:
        raise FileNotFoundError(f"No qiuzhao source found under {target}")
    # Feishu uses company + position as the stable identity. Keep the same
    # identity here so a collected row cannot overwrite a manually maintained
    # row merely because its URL or location differs.
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for feed in inputs:
        for row in feed["items"]:
            key = tuple(_text(row.get(name)).casefold() for name in ("company_name", "position"))
            if key in seen:
                continue
            seen.add(key)
            items.append(row)
    output = {
        "generated_at": next((feed.get("generated_at") for feed in inputs if feed.get("generated_at")), None),
        "source": "qiuzhao",
        "status": {
            "source": "qiuzhao", "status": "ok", "item_count": len(items),
            "upstream_source": "+".join(dict.fromkeys(str(feed["status"].get("upstream_source") or "") for feed in inputs)),
        },
        "items": items,
    }
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
