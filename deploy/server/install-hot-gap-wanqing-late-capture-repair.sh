#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-wanqing-late-capture-repair.tar.gz
state=/var/lib/hot-gap/daily-volume-state.json
daily_output=/var/www/hot-gap/data/daily-volume.json
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-wanqing-late-capture-repair"
files=(
  app/daily_volume.py
  deploy/server/install-hot-gap-wanqing-late-capture-repair.sh
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
shell_file="$project/deploy/server/install-hot-gap-wanqing-late-capture-repair.sh"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$shell_file" >/dev/null
else
  sed -i 's/\r$//' "$shell_file"
fi
chown root:root "$project/app/daily_volume.py" "$shell_file"
chmod 644 "$project/app/daily_volume.py"
chmod 755 "$shell_file"

cd "$project"
.venv/bin/python -m py_compile app/daily_volume.py
repair_output="$backup/repair-result.json"
.venv/bin/python -m app.daily_volume \
  --repair-wanqing-date 2026-09-13 \
  --report-date 2026-09-12 \
  --no-alert | tee "$repair_output"

.venv/bin/python - "$repair_output" "$state" <<'PY'
import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
repair = result["repair"]
assert repair["candidate_count"] == 108, repair
assert repair["corrected_from_rows"] == 108, repair
assert repair["corrected_from_overrides"] == 0, repair
assert repair["unresolved_count"] == 0, repair
assert result["date"] == "2026-09-12", result
assert result["qiuzhao_new"] == 201, result
assert result["qiuzhao_wanqing_new"] == 106, result
assert result["qiuzhao_source_new"].get("婉清购买表") == 106, result

state = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert sum(
    value == "2026-09-13"
    and state["qiuzhao_first_seen_source"].get(key) == "婉清购买表"
    for key, value in state["qiuzhao_first_seen"].items()
) == 0
PY

trap - EXIT
echo "Installed next-morning Wanqing source-date attribution"
echo "Corrected 2026-09-12 Qiuzhao to 201 total / 106 Wanqing records"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
