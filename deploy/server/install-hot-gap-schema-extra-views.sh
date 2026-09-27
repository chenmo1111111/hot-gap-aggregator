#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-schema-extra-views.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-schema-extra-views"

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
assert schema_diff(expected, actual) == []
assert schema_diff(expected, {**actual, "views": []}) == ["缺少基准视图：全部信息"]
print("Verified extra views are allowed while required baseline views remain locked")
PY

echo "Installed schema guard support for additional user-created views"
echo "Field names, field types, select options, and baseline views remain protected"
echo "No cron schedule, refresh process, service, database, or container was changed"
echo "Backup: $backup"
