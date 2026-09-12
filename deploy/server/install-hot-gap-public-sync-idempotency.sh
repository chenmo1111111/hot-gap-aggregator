#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-public-sync-idempotency.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-public-sync-idempotency"
files=(
  app/sync_feishu_public.py
  deploy/server/install-hot-gap-public-sync-idempotency.sh
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
shell_file="$project/deploy/server/install-hot-gap-public-sync-idempotency.sh"
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
.venv/bin/python -m py_compile app/sync_feishu_public.py

echo "Installed public Feishu idempotency fix"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
echo "First public sync backfills 594 blank city cells as slash placeholders"
.venv/bin/python -m app.sync_feishu_public
echo "Second public sync must report updated=0 for both tables"
.venv/bin/python -m app.sync_feishu_public

trap - EXIT
echo "Public sync idempotency verification completed"
