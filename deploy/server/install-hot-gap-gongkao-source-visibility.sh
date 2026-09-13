#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-gongkao-source-visibility.tar.gz
state=/var/lib/hot-gap/daily-volume-state.json
daily_output=/var/www/hot-gap/data/daily-volume.json
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-gongkao-source-visibility"
files=(
  app/daily_volume.py
  app/sync_feishu.py
  app/sync_feishu_public.py
  deploy/server/install-hot-gap-gongkao-source-visibility.sh
)

if [[ ! -d "$project" || ! -f "$archive" || ! -f "$state" ]]; then
  echo "Missing project, deployment archive, or daily-volume state" >&2
  exit 1
fi

mkdir -p "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    install -D -p "$project/$relative" "$backup/$relative"
  fi
done
install -D -p "$state" "$backup/state.json"
install -D -p "$daily_output" "$backup/daily-volume.json"

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      fi
    done
    install -p "$backup/state.json" "$state"
    install -p "$backup/daily-volume.json" "$daily_output"
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project"
shell_file="$project/deploy/server/install-hot-gap-gongkao-source-visibility.sh"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$shell_file" >/dev/null
else
  sed -i 's/\r$//' "$shell_file"
fi
chown root:root \
  "$project/app/daily_volume.py" \
  "$project/app/sync_feishu.py" \
  "$project/app/sync_feishu_public.py" \
  "$shell_file"
chmod 644 \
  "$project/app/daily_volume.py" \
  "$project/app/sync_feishu.py" \
  "$project/app/sync_feishu_public.py"
chmod 755 "$shell_file"

cd "$project"
.venv/bin/python -m py_compile \
  app/daily_volume.py app/sync_feishu.py app/sync_feishu_public.py
report_output="$backup/daily-volume-result.json"
.venv/bin/python -m app.daily_volume \
  --report-date 2026-09-12 --no-alert | tee "$report_output"

.venv/bin/python - "$report_output" <<'PY'
import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert result["gongkao_new"] == 235, result
counts = result["gongkao_source_new"]
expected = {
    "校招鸭事业单位购买表": 29,
    "婉清购买表分流": 103,
    "校招鸭home一次性基础层": 42,
    "粉笔": 60,
    "中公": 1,
}
assert {name: counts.get(name, 0) for name in expected} == expected, counts
assert sum(counts.values()) == 235, counts
PY

trap - EXIT
echo "Installed detailed Gongkao source labels and source-level daily counts"
echo "Feishu synchronization was not auto-started by this installer"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
