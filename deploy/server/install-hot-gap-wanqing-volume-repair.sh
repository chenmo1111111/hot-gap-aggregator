#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-wanqing-volume-repair.tar.gz
state=/var/lib/hot-gap/daily-volume-state.json
daily_output=/var/www/hot-gap/data/daily-volume.json
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-wanqing-volume-repair"
files=(
  app/daily_volume.py
  deploy/server/install-hot-gap-wanqing-volume-repair.sh
)

if [[ ! -d "$project" || ! -f "$archive" || ! -f "$state" ]]; then
  echo "Missing project, deployment archive, or daily-volume state" >&2
  exit 1
fi

mkdir -p "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    install -D -p "$project/$relative" "$backup/$relative"
  else
    mkdir -p "$backup/absent/$(dirname "$relative")"
    : > "$backup/absent/$relative"
  fi
done
install -D -p "$state" "$backup/state.json"
if [[ -f "$daily_output" ]]; then
  install -D -p "$daily_output" "$backup/daily-volume.json"
else
  : > "$backup/daily-output-was-absent"
fi

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      elif [[ -f "$backup/absent/$relative" ]]; then
        rm -f -- "$project/$relative"
      fi
    done
    install -p "$backup/state.json" "$state"
    if [[ -f "$backup/daily-volume.json" ]]; then
      install -p "$backup/daily-volume.json" "$daily_output"
    elif [[ -f "$backup/daily-output-was-absent" ]]; then
      rm -f -- "$daily_output"
    fi
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project"
shell_file="$project/deploy/server/install-hot-gap-wanqing-volume-repair.sh"
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
  --repair-wanqing-date 2026-09-12 \
  --repair-date-override recvtT12TSRTKZ=2026-08-31 \
  --repair-date-override recvtT12TST2oo=2026-08-31 \
  --no-alert | tee "$repair_output"

.venv/bin/python - "$repair_output" "$state" <<'PY'
import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
repair = result["repair"]
assert repair["candidate_count"] == 2775, repair
assert repair["corrected_from_rows"] == 2773, repair
assert repair["corrected_from_overrides"] == 2, repair
assert repair["unresolved_count"] == 0, repair
assert result["date"] == "2026-09-12", result
assert result["qiuzhao_new"] == 95, result
assert result["qiuzhao_wanqing_new"] == 0, result
counts = result["qiuzhao_source_new"]
assert counts.get("婉清购买表") == 0, counts
assert counts.get("国聘") == 1, counts
assert counts.get("国家大学生就业服务平台") == 83, counts
assert counts.get("高校就业网") == 11, counts
assert sum(counts.values()) == 95, counts

state = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
remaining = [
    key for key, value in state["qiuzhao_first_seen"].items()
    if value == "2026-09-12"
    and state["qiuzhao_first_seen_source"].get(key) == "婉清购买表"
]
assert not remaining, remaining[:10]
PY

trap - EXIT
echo "Installed Wanqing first-seen guard and repaired the 2026-09-12 daily-volume state"
echo "Corrected the report from Qiuzhao 2870 / Wanqing 2775 to Qiuzhao 95 / Wanqing 0"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
