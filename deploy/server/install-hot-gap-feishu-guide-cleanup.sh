#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-feishu-guide-cleanup.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-feishu-guide-cleanup"
files=(
  app/sync_feishu_public.py
  config/feishu_public_sync.yaml
  deploy/server/install-hot-gap-feishu-guide-cleanup.sh
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
shell_file="$project/deploy/server/install-hot-gap-feishu-guide-cleanup.sh"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$shell_file" >/dev/null
else
  sed -i 's/\r$//' "$shell_file"
fi
chown root:root \
  "$project/app/sync_feishu_public.py" \
  "$project/config/feishu_public_sync.yaml" \
  "$shell_file"
chmod 644 \
  "$project/app/sync_feishu_public.py" \
  "$project/config/feishu_public_sync.yaml"
chmod 755 "$shell_file"

cd "$project"
.venv/bin/python -m py_compile app/sync_feishu_public.py
.venv/bin/python - <<'PY'
from app.sync_feishu_public import load_public_config

config = load_public_config("config/feishu_public_sync.yaml")
assert config.get("instructions_table_enabled") is False, config
assert "instructions_table_name" not in config, config
print("Legacy instructions table sync is disabled")
PY

trap - EXIT
echo "Installed Feishu guide cleanup guard"
echo "The deleted legacy instructions tables will not be recreated by daily sync"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
