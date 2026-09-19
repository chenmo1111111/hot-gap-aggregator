#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-multi-digest.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-multi-digest"
files=(app/notify_gongkao_digest.py .env.example)

if [[ "${HOT_GAP_MULTI_DIGEST_LOCKED:-}" != 1 ]]; then
  exec /usr/bin/env HOT_GAP_MULTI_DIGEST_LOCKED=1 \
    /usr/bin/flock -E 200 -w 300 /run/lock/hot-gap-feishu-sync.lock /bin/bash "$0"
fi
if [[ ! -d "$project" || ! -f "$archive" ]]; then
  echo "Missing project or deployment archive" >&2
  exit 1
fi

mkdir -p "$backup"
for relative in "${files[@]}"; do
  install -D -p "$project/$relative" "$backup/$relative"
done

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
    done
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
from app.notify_gongkao_digest import digest_webhooks
assert digest_webhooks({
    'FEISHU_DIGEST_WEBHOOK': 'https://old.test',
    'FEISHU_DIGEST_WEBHOOKS': 'https://new.test,https://old.test',
}) == ['https://old.test', 'https://new.test']
PY

trap - EXIT
echo "Installed multi-group Gongkao digest support"
echo "No cron, refresh, service, or container was changed"
echo "Backup: $backup"
