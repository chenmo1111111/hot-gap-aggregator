"""Capture the final internal/public Qiuzhao Bitable schemas as YAML."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from app.feishu_schema_guard import canonical_schema
from app.sync_feishu import FeishuClient, load_config
from app.sync_feishu_public import load_public_config


def run(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="config/qiuzhao_feishu_schema.yaml")
    args = parser.parse_args(argv)
    internal = load_config(os.getenv("FEISHU_SYNC_CONFIG", "config/feishu_sync.yaml"))
    public = load_public_config(
        os.getenv("FEISHU_PUBLIC_SYNC_CONFIG", "config/feishu_public_sync.yaml")
    )
    app_id, app_secret = os.getenv("FEISHU_APP_ID", ""), os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID/FEISHU_APP_SECRET are required")
    tables = {
        "internal": (str(internal["app_token"]), str(internal["qiuzhao_table_id"])),
        "public": (str(public["app_token"]), str(public["qiuzhao_table_id"])),
    }
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "description": "Final user-approved Qiuzhao Bitable schemas; sync must stop on drift.",
        "tables": {},
    }
    with FeishuClient(app_id, app_secret, timeout=60) as client:
        for label, (app_token, table_id) in tables.items():
            payload["tables"][label] = {
                **canonical_schema(
                    client.list_fields(app_token, table_id),
                    client.list_views(app_token, table_id),
                ),
            }
    output = Path(args.output)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    temporary.replace(output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
