#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-remove-public-source.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-public-source-removal"
relative=app/sync_feishu_public.py

if [[ ! -d "$project" || ! -f "$archive" ]]; then
  echo "Missing project or deployment archive" >&2
  exit 1
fi

mkdir -p "$backup/app"
install -D -p "$project/$relative" "$backup/$relative"

cd "$project"
.venv/bin/python - "$backup" <<'PY'
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from app.sync_feishu import FeishuClient, _cell_text
from app.sync_feishu_public import load_public_config

load_dotenv("/opt/hot-gap-aggregator/.env")
config = load_public_config("config/feishu_public_sync.yaml")
with FeishuClient(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"]) as client:
    rows = client.list_records(config["app_token"], config["gongkao_table_id"])
snapshot = [
    {
        "record_id": str(row.get("record_id") or ""),
        "sync_id": _cell_text((row.get("fields") or {}).get("同步ID")).strip(),
        "source": _cell_text((row.get("fields") or {}).get("来源")).strip(),
    }
    for row in rows
]
unsafe = [row for row in snapshot if not row["sync_id"] or row["source"] == "手动"]
if unsafe:
    raise SystemExit(f"Refusing to remove 来源: {len(unsafe)} manual/unidentified rows")
path = Path(sys.argv[1]) / "public-source-before-removal.json"
path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
path.chmod(0o600)
print(f"Backed up {len(snapshot)} public source labels to {path}")
PY

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project" "$relative"
chown root:root "$project/$relative"
chmod 644 "$project/$relative"
.venv/bin/python -m py_compile "$relative"
.venv/bin/python - <<'PY'
from app.sync_feishu_public import GONGKAO_DEPRECATED_FIELDS, GONGKAO_SCHEMA
assert "来源" not in {field["field_name"] for field in GONGKAO_SCHEMA}
assert "来源" in GONGKAO_DEPRECATED_FIELDS
PY

trap - EXIT
echo "Installed code to remove public Gongkao 来源 column on next public sync"
echo "No cron schedule, service, refresh, or container was changed"
echo "Backup: $backup"
