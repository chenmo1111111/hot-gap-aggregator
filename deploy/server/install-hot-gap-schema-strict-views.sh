#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-schema-strict-views.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-schema-strict-views"

mkdir -p "$backup/app"
install -p "$project/app/feishu_schema_guard.py" "$backup/app/feishu_schema_guard.py"

restore() {
  install -o root -g root -m 644 \
    "$backup/app/feishu_schema_guard.py" "$project/app/feishu_schema_guard.py"
}
trap 'status=$?; if (( status != 0 )); then restore; fi; exit "$status"' EXIT

tar -xzf "$archive" -C "$project" \
  hot-gap-aggregator/app/feishu_schema_guard.py \
  --strip-components=1
chown root:root "$project/app/feishu_schema_guard.py"
chmod 644 "$project/app/feishu_schema_guard.py"

cd "$project"
.venv/bin/python -m py_compile app/feishu_schema_guard.py
.venv/bin/python - <<'PY'
from app.feishu_schema_guard import schema_diff

expected = {
    "fields": [{"name": "公司名称", "type": 1}],
    "views": [{"name": "全部信息", "type": "grid"}],
}
actual = {
    "fields": [{"name": "公司名称", "type": 1}],
    "views": [
        {"name": "全部信息", "type": "grid"},
        {"name": "江苏", "type": "grid"},
    ],
}
differences = schema_diff(expected, actual)
assert differences and "视图列表变化" in differences[0]
print("Verified unexpected extra views remain blocking")
PY

echo "Restored strict Feishu view-list schema locking"
echo "Any unconfirmed view addition, deletion, rename, reorder, or type change stops writes"
echo "No cron schedule, refresh process, service, database, or container was changed"
echo "Backup: $backup"
