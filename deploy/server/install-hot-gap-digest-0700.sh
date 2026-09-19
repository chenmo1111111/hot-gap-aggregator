#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-digest-0700.tar.gz
cron_source=deploy/server/hot-gap-daily-volume.cron
cron_target=/etc/cron.d/hot-gap-daily-volume
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-digest-0700"
files=(app/notify_gongkao_digest.py .env.example "$cron_source")

if [[ ! -d "$project" || ! -f "$archive" ]]; then
  echo "Missing project or deployment archive" >&2
  exit 1
fi
mkdir -p "$backup"
for relative in "${files[@]}"; do
  install -D -p "$project/$relative" "$backup/$relative"
done
install -p "$cron_target" "$backup/hot-gap-daily-volume.cron.installed"

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
    done
    install -o root -g root -m 644 \
      "$backup/hot-gap-daily-volume.cron.installed" "$cron_target"
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project" "${files[@]}"
for relative in "${files[@]}"; do
  chown root:root "$project/$relative"
  chmod 644 "$project/$relative"
done
cd "$project"
.venv/bin/python -m py_compile app/notify_gongkao_digest.py
.venv/bin/python - <<'PY'
from datetime import datetime
from app.notify_gongkao_digest import CHINA_TZ, digest_send_allowed
assert not digest_send_allowed(datetime(2026, 9, 19, 5, 0, tzinfo=CHINA_TZ), after_hour=7)
assert digest_send_allowed(datetime(2026, 9, 19, 7, 0, tzinfo=CHINA_TZ), after_hour=7)
PY

install -o root -g root -m 644 "$cron_source" "$cron_target"
grep -Fq "0 7 * * * root" "$cron_target"
grep -Fq -- "-m app.daily_volume" "$cron_target"
grep -Fq -- "-m app.notify_gongkao_digest" "$cron_target"
grep -Fq "GONGKAO_DIGEST_AFTER_HOUR=7" "$cron_target"
if systemctl is-active --quiet cron.service; then
  systemctl reload cron.service 2>/dev/null || systemctl restart cron.service
fi

trap - EXIT
echo "Installed 07:00 daily volume plus multi-group Gongkao digest schedule"
echo "The recurring refresh cron was preserved; early refreshes are time-gated"
echo "No collector, refresh process, or container was started"
echo "Backup: $backup"
