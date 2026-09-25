"""Immutable Qiuzhao Bitable schema snapshots and pre-write validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


SELECT_TYPES = {3, 4}


class SchemaDriftError(RuntimeError):
    pass


def canonical_schema(fields: list[dict[str, Any]], views: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_fields = []
    for field in fields:
        item: dict[str, Any] = {
            "name": str(field.get("field_name") or ""),
            "type": int(field.get("type") or 0),
            "primary": bool(field.get("is_primary")),
        }
        if item["type"] in SELECT_TYPES:
            prop = field.get("property") if isinstance(field.get("property"), Mapping) else {}
            item["options"] = [
                str(option.get("name") or "") for option in prop.get("options") or []
                if isinstance(option, Mapping) and str(option.get("name") or "")
            ]
        normalized_fields.append(item)
    return {
        "fields": normalized_fields,
        "views": [
            {
                "name": str(view.get("view_name") or view.get("name") or ""),
                "type": str(view.get("view_type") or "grid"),
            }
            for view in views
        ],
    }


def load_snapshot(path: str | Path, profile: str) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    profiles = raw.get("tables") if isinstance(raw, Mapping) else None
    value = profiles.get(profile) if isinstance(profiles, Mapping) else None
    if not isinstance(value, Mapping):
        raise ValueError(f"schema snapshot has no {profile} table")
    return dict(value)


def schema_diff(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    differences: list[str] = []
    expected_fields = {str(row.get("name")): row for row in expected.get("fields") or []}
    actual_fields = {str(row.get("name")): row for row in actual.get("fields") or []}
    missing = sorted(expected_fields.keys() - actual_fields.keys())
    extra = sorted(actual_fields.keys() - expected_fields.keys())
    if missing:
        differences.append("缺少字段：" + "、".join(missing))
    if extra:
        differences.append("新增字段：" + "、".join(extra))
    for name in sorted(expected_fields.keys() & actual_fields.keys()):
        wanted, live = expected_fields[name], actual_fields[name]
        if int(wanted.get("type") or 0) != int(live.get("type") or 0):
            differences.append(f"字段{name}类型 {live.get('type')} != {wanted.get('type')}")
        # Feishu's list-fields endpoint has returned contradictory is_primary
        # flags for the same live table across adjacent reads.  The primary
        # designation is not writable by this sync and is therefore recorded
        # for audit only, not used as a blocking comparison.
        if int(wanted.get("type") or 0) in SELECT_TYPES:
            if list(wanted.get("options") or []) != list(live.get("options") or []):
                differences.append(f"字段{name}选项列表变化")
    expected_views = [(str(row.get("name")), str(row.get("type") or "grid")) for row in expected.get("views") or []]
    actual_views = [(str(row.get("name")), str(row.get("type") or "grid")) for row in actual.get("views") or []]
    if expected_views != actual_views:
        differences.append(
            "视图列表变化：线上=" + "、".join(name for name, _ in actual_views)
            + "；基准=" + "、".join(name for name, _ in expected_views)
        )
    return differences


def validate_live_schema(client: Any, app_token: str, table_id: str, expected: Mapping[str, Any]) -> None:
    actual = canonical_schema(
        client.list_fields(app_token, table_id), client.list_views(app_token, table_id)
    )
    differences = schema_diff(expected, actual)
    if differences:
        raise SchemaDriftError("；".join(differences))


def sanitize_fields(fields: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    """Drop deleted fields and unknown select choices; never mutate live schema."""
    definitions = {str(row.get("name")): row for row in expected.get("fields") or []}
    clean: dict[str, Any] = {}
    for name, value in fields.items():
        definition = definitions.get(name)
        if not definition:
            continue
        field_type = int(definition.get("type") or 0)
        if field_type == 3:
            allowed = set(definition.get("options") or [])
            if value in allowed:
                clean[name] = value
            elif name == "来源" and str(value or "").startswith("自动") and "自动" in allowed:
                # The schema is intentionally locked, so a newly introduced
                # collector must not create another select option. Keep the
                # generic managed marker instead of dropping provenance to
                # blank, otherwise the next sync mistakes the row for a
                # user-maintained record and can never evict it for capacity.
                clean[name] = "自动"
            else:
                clean[name] = None
        elif field_type == 4:
            allowed = set(definition.get("options") or [])
            values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
            clean[name] = [item for item in values if item in allowed]
        else:
            clean[name] = value
    return clean
