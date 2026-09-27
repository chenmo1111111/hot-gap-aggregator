#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-daily-broadcast-clarity.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-daily-broadcast-clarity"

mkdir -p "$backup/app"
install -p "$project/app/daily_volume.py" "$backup/app/daily_volume.py"

restore() {
  install -o root -g root -m 644 "$backup/app/daily_volume.py" "$project/app/daily_volume.py"
}
trap 'status=$?; if (( status != 0 )); then restore; fi; exit "$status"' EXIT

tar -xzf "$archive" -C "$project" \
  hot-gap-aggregator/app/daily_volume.py \
  --strip-components=1
chown root:root "$project/app/daily_volume.py"
chmod 644 "$project/app/daily_volume.py"

cd "$project"
.venv/bin/python -m py_compile app/daily_volume.py
.venv/bin/python - <<'PY'
from app.daily_volume import build_daily_broadcast

sample = build_daily_broadcast({
    "date": "2026-09-26",
    "qiuzhao_new": 89,
    "gongkao_new": 0,
    "qiuzhao_captured_new": 85,
    "gongkao_captured_new": 10,
    "qiuzhao_published_new": 89,
    "gongkao_public_first_seen_new": 0,
    "qiuzhao_captured_source_new": {"国家大学生就业服务平台": 43},
    "qiuzhao_published_source_new": {"国家大学生就业服务平台": 40},
    "gongkao_captured_source_new": {"粉笔": 10},
    "gongkao_public_first_seen_source_new": {"粉笔": 0},
    "qiuzhao_wanqing_new": 0,
    "qiuzhao_xiaozhaoya_new": 0,
    "qiuzhao_other_new": 89,
    "gongkao_government_new": 0,
    "gongkao_sheet_new": 0,
    "gongkao_other_new": 0,
})
assert "公考·公开表‘首次收录’为昨日 0 条" in sample
assert "公考·后台采集器昨日首次新见候选 10 条" in sample
assert sample.index("公开表‘首次收录’") < sample.index("后台采集器昨日首次新见候选 10")
print(sample)
PY

echo "Installed daily broadcast wording that separates public-table rows from raw collector candidates"
echo "No cron schedule, refresh process, service, database, or container was changed"
echo "Backup: $backup"
