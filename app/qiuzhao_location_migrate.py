"""One-off, resumable migration of Qiuzhao 工作地点 to multi-select.

The command intentionally operates outside the recurring sync.  ``pilot`` uses
a temporary field, while ``apply`` reads immutable backups instead of the live
column so a partial conversion can always resume safely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv

from app.pipeline.qiuzhao_location import location_options, split_work_locations
from app.sync_feishu import BATCH_SIZE, FeishuClient, _cell_text, load_config
from app.sync_feishu_public import load_public_config


FIELD = "工作地点"
PILOT_FIELD = "工作地点_迁移验证"


def _atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tables() -> dict[str, tuple[str, str]]:
    internal = load_config(os.getenv("FEISHU_SYNC_CONFIG", "config/feishu_sync.yaml"))
    public = load_public_config(
        os.getenv("FEISHU_PUBLIC_SYNC_CONFIG", "config/feishu_public_sync.yaml")
    )
    return {
        "internal": (str(internal["app_token"]), str(internal["qiuzhao_table_id"])),
        "public": (str(public["app_token"]), str(public["qiuzhao_table_id"])),
    }


def backup(client: FeishuClient, directory: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(), "tables": {},
    }
    for label, (app_token, table_id) in _tables().items():
        fields = client.list_fields(app_token, table_id)
        location = next((row for row in fields if row.get("field_name") == FIELD), None)
        if not location:
            raise RuntimeError(f"{label} 秋招表缺少{FIELD}")
        records = client.list_records(app_token, table_id)
        payload = {
            "created_at": manifest["created_at"], "label": label, "table_id": table_id,
            "record_count": len(records), "location_field": location,
            "rows": [
                {
                    "record_id": str(row.get("record_id") or ""),
                    "company": _cell_text((row.get("fields") or {}).get("公司名称")),
                    "position": _cell_text((row.get("fields") or {}).get("招聘岗位")),
                    "location_raw": _cell_text((row.get("fields") or {}).get(FIELD)),
                }
                for row in records
            ],
        }
        path = directory / f"{label}-locations.json"
        _atomic(path, payload)
        manifest["tables"][label] = {
            "table_id": table_id, "record_count": len(records),
            "file": path.name, "sha256": _sha(path),
        }
    _atomic(directory / "manifest.json", manifest)
    return manifest


def load_plan(directory: Path) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    all_values: list[str] = []
    for label in ("internal", "public"):
        payload = json.loads((directory / f"{label}-locations.json").read_text(encoding="utf-8"))
        rows = payload.get("rows") or []
        planned = [
            {
                "record_id": str(row.get("record_id") or ""),
                "before": str(row.get("location_raw") or ""),
                "after": split_work_locations(row.get("location_raw")),
                "company": str(row.get("company") or ""),
                "position": str(row.get("position") or ""),
            }
            for row in rows
        ]
        tables[label] = planned
        all_values.extend(row["before"] for row in planned)
    options = location_options(all_values)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "options": options, "option_count": len(options), "tables": tables,
    }
    _atomic(directory / "location-plan.json", result)
    return result


def _field(client: FeishuClient, app_token: str, table_id: str, name: str) -> dict[str, Any] | None:
    return next(
        (row for row in client.list_fields(app_token, table_id) if row.get("field_name") == name),
        None,
    )


def pilot(client: FeishuClient, directory: Path, *, size: int = 100) -> dict[str, Any]:
    plan = load_plan(directory)
    result: dict[str, Any] = {"option_count": len(plan["options"]), "tables": {}}
    definition = {
        "field_name": PILOT_FIELD, "type": 4,
        "property": {"options": [{"name": name} for name in plan["options"]]},
    }
    for label, (app_token, table_id) in _tables().items():
        if _field(client, app_token, table_id, PILOT_FIELD):
            raise RuntimeError(f"{label} 已存在 {PILOT_FIELD}，请先人工确认上次试跑")
        client.create_field(app_token, table_id, definition)
        samples = [row for row in plan["tables"][label] if len(row["after"]) >= 2][:size]
        if len(samples) < size:
            samples.extend(
                row for row in plan["tables"][label]
                if row["after"] and row not in samples
            )
            samples = samples[:size]
        try:
            for start in range(0, len(samples), BATCH_SIZE):
                client.batch_update(app_token, table_id, [
                    {"record_id": row["record_id"], "fields": {PILOT_FIELD: row["after"]}}
                    for row in samples[start:start + BATCH_SIZE]
                ])
            records = {str(row.get("record_id")): row for row in client.list_records(app_token, table_id)}
            mismatches = [
                row for row in samples
                if set(_cell_text((records[row["record_id"]].get("fields") or {}).get(PILOT_FIELD)).split("、"))
                != set(row["after"])
            ]
            if mismatches:
                # Multi-select is normally returned as a JSON list, which _cell_text
                # concatenates.  Re-read using the raw value for an exact comparison.
                mismatches = [
                    row for row in samples
                    if set((records[row["record_id"]].get("fields") or {}).get(PILOT_FIELD) or [])
                    != set(row["after"])
                ]
            if mismatches:
                raise RuntimeError(f"{label} 试跑校验失败 {len(mismatches)} 行")
            result["tables"][label] = {
                "tested": len(samples), "mismatches": 0, "samples": samples[:5],
            }
        finally:
            field = _field(client, app_token, table_id, PILOT_FIELD)
            if field and field.get("field_id"):
                client.delete_field(app_token, table_id, str(field["field_id"]))
    _atomic(directory / "pilot-result.json", result)
    return result


def apply(client: FeishuClient, directory: Path) -> dict[str, Any]:
    plan = load_plan(directory)
    options = [{"name": name} for name in plan["options"]]
    result: dict[str, Any] = {"option_count": len(options), "tables": {}}
    for label, (app_token, table_id) in _tables().items():
        field = _field(client, app_token, table_id, FIELD)
        if not field or not field.get("field_id"):
            raise RuntimeError(f"{label} 缺少 {FIELD}")
        if int(field.get("type") or 0) not in {1, 4}:
            raise RuntimeError(f"{label} {FIELD} 类型异常：{field.get('type')}")
        client.update_field(app_token, table_id, str(field["field_id"]), {
            "field_name": FIELD, "type": 4, "property": {"options": options},
        })
        checkpoint_path = directory / f"{label}-checkpoint.json"
        completed = 0
        if checkpoint_path.exists():
            completed = int(json.loads(checkpoint_path.read_text(encoding="utf-8")).get("completed", 0))
        rows = plan["tables"][label]
        for start in range(completed, len(rows), BATCH_SIZE):
            batch = rows[start:start + BATCH_SIZE]
            client.batch_update(app_token, table_id, [
                {"record_id": row["record_id"], "fields": {FIELD: row["after"]}}
                for row in batch
            ])
            completed = start + len(batch)
            _atomic(checkpoint_path, {"completed": completed, "total": len(rows)})
        result["tables"][label] = {"updated": completed, "total": len(rows)}
    _atomic(directory / "apply-result.json", result)
    return result


def verify(client: FeishuClient, directory: Path) -> dict[str, Any]:
    plan = load_plan(directory)
    result: dict[str, Any] = {"tables": {}}
    for label, (app_token, table_id) in _tables().items():
        expected = {row["record_id"]: set(row["after"]) for row in plan["tables"][label]}
        mismatches: list[str] = []
        for record in client.list_records(app_token, table_id):
            record_id = str(record.get("record_id") or "")
            if record_id not in expected:
                continue
            raw = (record.get("fields") or {}).get(FIELD) or []
            actual = set(raw if isinstance(raw, list) else [str(raw)])
            if actual != expected[record_id]:
                mismatches.append(record_id)
        result["tables"][label] = {
            "expected": len(expected), "mismatch_count": len(mismatches),
            "mismatch_ids": mismatches[:20],
        }
    _atomic(directory / "verify-result.json", result)
    return result


def run(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("backup", "plan", "pilot", "apply", "verify"))
    parser.add_argument("--directory", required=True)
    parser.add_argument("--pilot-size", type=int, default=100)
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    if args.command == "plan":
        plan = load_plan(directory)
        print(json.dumps({
            "option_count": plan["option_count"],
            "tables": {name: len(rows) for name, rows in plan["tables"].items()},
        }, ensure_ascii=False))
        return 0
    app_id, app_secret = os.getenv("FEISHU_APP_ID", ""), os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID/FEISHU_APP_SECRET are required")
    with FeishuClient(app_id, app_secret, timeout=60) as client:
        result = {
            "backup": lambda: backup(client, directory),
            "pilot": lambda: pilot(client, directory, size=args.pilot_size),
            "apply": lambda: apply(client, directory),
            "verify": lambda: verify(client, directory),
        }[args.command]()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
