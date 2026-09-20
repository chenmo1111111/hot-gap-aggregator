#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-qiuzhao-schema-capacity.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-qiuzhao-schema-capacity"
files=(
  app/feishu_schema_guard.py
  app/feishu_schema_snapshot.py
  app/qiuzhao_location_migrate.py
  app/pipeline/prune.py
  app/pipeline/qiuzhao_capacity.py
  app/pipeline/qiuzhao_location.py
  app/sync_feishu.py
  app/sync_feishu_public.py
  config/qiuzhao_feishu_schema.yaml
  config/retention.yaml
)

[[ -f "$archive" ]] || { echo "Missing $archive" >&2; exit 1; }
/usr/bin/install -d -m 755 "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    /usr/bin/install -D -m 644 "$project/$relative" "$backup/$relative"
  fi
done

stage="$(mktemp -d /tmp/hot-gap-qiuzhao-schema.XXXXXX)"
trap '/usr/bin/rm -rf -- "$stage"' EXIT
/usr/bin/tar -xzf "$archive" -C "$stage"
for relative in "${files[@]}"; do
  source_file="$stage/hot-gap-aggregator/$relative"
  [[ -f "$source_file" ]] || { echo "Archive missing $relative" >&2; exit 1; }
  /usr/bin/install -D -o root -g root -m 644 "$source_file" "$project/$relative"
done

cd "$project"
.venv/bin/python -m py_compile \
  app/feishu_schema_guard.py app/feishu_schema_snapshot.py \
  app/qiuzhao_location_migrate.py app/pipeline/prune.py \
  app/pipeline/qiuzhao_capacity.py app/pipeline/qiuzhao_location.py \
  app/sync_feishu.py app/sync_feishu_public.py
.venv/bin/python - <<'PY'
import yaml
from pathlib import Path

snapshot = yaml.safe_load(Path("config/qiuzhao_feishu_schema.yaml").read_text(encoding="utf-8"))
for name in ("internal", "public"):
    table = snapshot["tables"][name]
    location = next(field for field in table["fields"] if field["name"] == "工作地点")
    assert location["type"] == 4
    assert len(location["options"]) == 433
print("Schema snapshot verified: internal/public 工作地点 are 433-option multi-select fields")
PY

echo "Installed Qiuzhao multi-select mapping, capacity guard, retention, and immutable schema lock"
echo "No cron schedule, service, refresh process, or container was changed or started"
echo "Backup: $backup"
