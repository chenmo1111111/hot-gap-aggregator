#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-remove-public-id.tar.gz
registry=/var/lib/hot-gap/gongkao-public-managed-records.json
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-public-id-removal"
files=(
  app/sync_feishu.py
  app/sync_feishu_public.py
  app/pipeline/public_sync_registry.py
  app/pipeline/yingjie_requirement.py
  app/pipeline/gongkao_enrich.py
)

if [[ "${HOT_GAP_PUBLIC_ID_LOCKED:-}" != 1 ]]; then
  exec /usr/bin/env HOT_GAP_PUBLIC_ID_LOCKED=1 \
    /usr/bin/flock -E 200 -w 300 /run/lock/hot-gap-feishu-sync.lock "$0"
fi
if [[ ! -d "$project" || ! -f "$archive" ]]; then
  echo "Missing project or deployment archive" >&2
  exit 1
fi

mkdir -p "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    install -D -p "$project/$relative" "$backup/$relative"
  fi
done

cd "$project"
.venv/bin/python - "$backup" "$registry" <<'PY'
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from app.sync_feishu import FeishuClient, _cell_text
from app.sync_feishu_public import load_public_config

load_dotenv('/opt/hot-gap-aggregator/.env')
config = load_public_config('config/feishu_public_sync.yaml')
app_token = str(config['app_token'])
table_id = str(config['gongkao_table_id'])
with FeishuClient(os.environ['FEISHU_APP_ID'], os.environ['FEISHU_APP_SECRET']) as client:
    fields = client.list_fields(app_token, table_id)
    rows = client.list_records(app_token, table_id)
if '同步ID' not in {str(field.get('field_name') or '') for field in fields}:
    raise SystemExit('Public 同步ID column is already absent; refusing a second migration')
snapshot = [{
    'record_id': str(row.get('record_id') or ''),
    'sync_id': _cell_text((row.get('fields') or {}).get('同步ID')).strip(),
} for row in rows]
if any(not row['record_id'] or not row['sync_id'] for row in snapshot):
    raise SystemExit('Public Base has manual/unidentified rows; refusing to migrate automatically')
path = Path(sys.argv[1]) / 'public-id-before-removal.json'
path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
path.chmod(0o600)
registry = Path(sys.argv[2])
if registry.exists():
    existing = json.loads(registry.read_text(encoding='utf-8'))
    if (existing.get('app_token') != app_token or existing.get('table_id') != table_id
            or set(existing.get('record_ids', [])) != {row['record_id'] for row in snapshot}):
        raise SystemExit('Existing private registry differs from live Base; refusing to overwrite')
print(f'Backed up {len(snapshot)} managed public row IDs to {path}')
PY

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring runtime files from $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      fi
    done
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project" "${files[@]}"
for relative in "${files[@]}"; do
  chown root:root "$project/$relative"
  chmod 644 "$project/$relative"
  .venv/bin/python -m py_compile "$relative"
done
.venv/bin/python - "$backup/public-id-before-removal.json" "$registry" <<'PY'
import json
import sys
from pathlib import Path

from app.pipeline.public_sync_registry import PublicSyncRegistry
from app.sync_feishu_public import GONGKAO_DEPRECATED_FIELDS, GONGKAO_SCHEMA
from app.sync_feishu_public import load_public_config

config = load_public_config('config/feishu_public_sync.yaml')
snapshot = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
registry = PublicSyncRegistry(sys.argv[2], str(config['app_token']), str(config['gongkao_table_id']))
registry.record_ids.update(row['record_id'] for row in snapshot)
registry.save()
assert '同步ID' not in {field['field_name'] for field in GONGKAO_SCHEMA}
assert '同步ID' in GONGKAO_DEPRECATED_FIELDS
assert len(registry.record_ids) == len(snapshot)
print(f'Created root-only private ownership registry: {len(registry.record_ids)} rows')
PY

trap - EXIT
echo "Installed public-ID-free Gongkao sync code; the next public sync removes 同步ID"
echo "No cron, service, refresh, or container was changed"
echo "Backup: $backup"
