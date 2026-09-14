#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-purchased-0600.tar.gz
state=/var/lib/hot-gap/daily-volume-state.json
daily_output=/var/www/hot-gap/data/daily-volume.json
stamp=$(date +%Y%m%d-%H%M%S)
backup="$project/.deploy-backups/${stamp}-before-purchased-0600"
files=(
  app/daily_volume.py
  app/pipeline/purchased_classify.py
  deploy/server/install-hot-gap-purchased-0600.sh
)

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this installer as root" >&2
  exit 1
fi
if [[ ! -d "$project" || ! -f "$archive" || ! -f "$state" || ! -f "$daily_output" ]]; then
  echo "Missing project, archive, daily-volume state, or website output" >&2
  exit 1
fi

mkdir -p "$backup"
for file in "${files[@]}"; do
  if [[ -f "$project/$file" ]]; then
    install -D -p "$project/$file" "$backup/$file"
  fi
done
install -D -p "$state" "$backup/daily-volume-state.json"
install -D -p "$daily_output" "$backup/daily-volume.json"

restore() {
  for file in "${files[@]}"; do
    if [[ -f "$backup/$file" ]]; then
      install -D -p "$backup/$file" "$project/$file"
    fi
  done
  install -p "$backup/daily-volume-state.json" "$state"
  install -p "$backup/daily-volume.json" "$daily_output"
}
trap 'echo "Deployment failed; restoring $backup" >&2; restore' ERR

tar -xzf "$archive" -C "$project" "${files[@]}"
dos2unix "$project/deploy/server/install-hot-gap-purchased-0600.sh" >/dev/null 2>&1 || true
chown root:root "$project/app/daily_volume.py" \
  "$project/app/pipeline/purchased_classify.py" \
  "$project/deploy/server/install-hot-gap-purchased-0600.sh"
chmod 644 "$project/app/daily_volume.py" "$project/app/pipeline/purchased_classify.py"
chmod 755 "$project/deploy/server/install-hot-gap-purchased-0600.sh"

cd "$project"
.venv/bin/python -m py_compile app/daily_volume.py app/pipeline/purchased_classify.py
result=$(
  .venv/bin/python -m app.daily_volume \
    --repair-gongkao-wanqing-date 2026-09-14 \
    --report-date 2026-09-13 \
    --repair-date-override purchased:wanqing_feishu:recvv6FcV5MCkR=2026-09-13 \
    --repair-date-override purchased:wanqing_feishu:recvv6FcV5G5oK=2026-09-13 \
    --no-alert
)
printf '%s\n' "$result"
.venv/bin/python -c 'import json,sys; value=json.loads(sys.argv[1]); repair=value["gongkao_repair"]; assert repair["corrected_from_overrides"] == 2; assert value["date"] == "2026-09-13"; assert value["qiuzhao_new"] == 234; assert value["gongkao_new"] == 59' "$result"

trap - ERR
echo "Installed purchased-table 06:00 support and repaired the two Sep-13 Gongkao routes"
echo "S1 daily broadcast remains at 07:00; no cron, service, refresh, or container was changed"
echo "Backup: $backup"
