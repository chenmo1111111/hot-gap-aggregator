#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-qiuzhao-capacity-order.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-qiuzhao-capacity-order"
files=(
  app/feishu_schema_guard.py
  app/sync_feishu.py
  app/sync_feishu_public.py
)

[[ -f "$archive" ]] || { echo "Missing $archive" >&2; exit 1; }
/usr/bin/install -d -m 755 "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    /usr/bin/install -D -m 644 "$project/$relative" "$backup/$relative"
  fi
done

stage="$(mktemp -d /tmp/hot-gap-qiuzhao-capacity-order.XXXXXX)"
trap '/usr/bin/rm -rf -- "$stage"' EXIT
/usr/bin/tar -xzf "$archive" -C "$stage"
for relative in "${files[@]}"; do
  source_file="$stage/hot-gap-aggregator/$relative"
  [[ -f "$source_file" ]] || { echo "Archive missing $relative" >&2; exit 1; }
  /usr/bin/install -D -o root -g root -m 644 "$source_file" "$project/$relative"
done

cd "$project"
.venv/bin/python -m py_compile "${files[@]}"
.venv/bin/python - <<'PY'
from app.feishu_schema_guard import sanitize_fields
from app.sync_feishu import diff_records, is_managed_record

schema = {"fields": [{"name": "来源", "type": 3, "options": ["自动", "手动"]}]}
assert sanitize_fields({"来源": "自动·编程导航"}, schema)["来源"] == "自动"
assert is_managed_record({"同步ID": "company|role", "来源": ""})
assert not is_managed_record({"同步ID": "manual|role", "来源": "手动"})
source = [{"同步ID": "company|role", "来源": "自动"}]
existing = [
    {"record_id": "keep", "fields": {"同步ID": "company|role", "来源": "自动"}},
    {"record_id": "duplicate", "fields": {"同步ID": "company|role", "来源": "自动"}},
]
assert diff_records(source, existing)[2] == ["duplicate"]
print("Verified managed-row recovery, duplicate pruning, and locked-source fallback")
PY

echo "Installed Qiuzhao capacity-safe delete-before-create synchronization"
echo "Duplicate and unkeyed automatic rows are pruned; explicit manual rows remain protected"
echo "No refresh, cron schedule, service, database, or container was started or changed"
echo "Backup: $backup"
