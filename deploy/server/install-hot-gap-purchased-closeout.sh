#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-purchased-closeout.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-purchased-closeout"
files=(
  app/daily_volume.py
  app/sync_feishu.py
  deploy/server/install-hot-gap-purchased-closeout.sh
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

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      fi
    done
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project"
shell_file="$project/deploy/server/install-hot-gap-purchased-closeout.sh"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$shell_file"
else
  sed -i 's/\r$//' "$shell_file"
fi
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    chown root:root "$project/$relative"
    chmod 644 "$project/$relative"
  fi
done

cd "$project"
.venv/bin/python -m py_compile app/daily_volume.py app/sync_feishu.py

echo "Installed purchased-table closeout fixes"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
echo "Recomputing today's source breakdown and repairing the one skipped Qiuzhao row"
.venv/bin/python -m app.daily_volume
.venv/bin/python -m app.sync_feishu
.venv/bin/python -m app.sync_feishu_public

trap - EXIT
echo "Closeout verification completed"
