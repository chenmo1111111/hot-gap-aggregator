#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-daily-volume-yesterday.tar.gz
cron_source=deploy/server/hot-gap-daily-volume.cron
cron_target=/etc/cron.d/hot-gap-daily-volume
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-daily-volume-yesterday"
files=(
  app/daily_volume.py
  deploy/server/hot-gap-daily-volume.cron
  deploy/server/install-hot-gap-daily-volume-yesterday.sh
)

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
if [[ -f "$cron_target" ]]; then
  install -p "$cron_target" "$backup/hot-gap-daily-volume.cron.installed"
else
  : > "$backup/cron-was-absent"
fi

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      fi
    done
    if [[ -f "$backup/hot-gap-daily-volume.cron.installed" ]]; then
      install -o root -g root -m 644 "$backup/hot-gap-daily-volume.cron.installed" "$cron_target"
    elif [[ -f "$backup/cron-was-absent" ]]; then
      rm -f -- "$cron_target"
    fi
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project"
for shell_file in \
  "$project/$cron_source" \
  "$project/deploy/server/install-hot-gap-daily-volume-yesterday.sh"
do
  if command -v dos2unix >/dev/null 2>&1; then
    dos2unix "$shell_file" >/dev/null
  else
    sed -i 's/\r$//' "$shell_file"
  fi
done

chown root:root "$project/app/daily_volume.py" "$project/$cron_source" \
  "$project/deploy/server/install-hot-gap-daily-volume-yesterday.sh"
chmod 644 "$project/app/daily_volume.py" "$project/$cron_source"
chmod 755 "$project/deploy/server/install-hot-gap-daily-volume-yesterday.sh"

cd "$project"
.venv/bin/python -m py_compile app/daily_volume.py
.venv/bin/python - <<'PY'
from datetime import datetime, timedelta
from app.daily_volume import CHINA_TZ, previous_day_window

first = datetime(2026, 9, 13, 7, 0, tzinfo=CHINA_TZ)
second = first + timedelta(days=1)
first_start, first_end = previous_day_window(first)
second_start, second_end = previous_day_window(second)
assert first_end == second_start
assert first_end - first_start == second_end - second_start == timedelta(hours=24)
PY

install -o root -g root -m 644 "$cron_source" "$cron_target"
grep -Fq "0 7 * * * root" "$cron_target"
grep -Fq "DAILY_VOLUME_ALERT_AFTER_HOUR=7" "$cron_target"
if systemctl is-active --quiet cron.service; then
  systemctl reload cron.service 2>/dev/null || systemctl restart cron.service
fi

trap - EXIT
echo "Installed previous-complete-day daily volume broadcast at 07:00 Asia/Shanghai"
echo "The existing hot-gap-feishu-refresh root cron was preserved"
echo "No collector, refresh process, or container was started"
echo "Backup: $backup"
