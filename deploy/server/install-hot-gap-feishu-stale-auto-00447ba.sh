#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-feishu-stale-auto-00447ba.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-feishu-stale-auto-00447ba"
files=(
  app/sync_feishu_public.py
  deploy/server/install-hot-gap-feishu-stale-auto-00447ba.sh
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
shell_file="$project/deploy/server/install-hot-gap-feishu-stale-auto-00447ba.sh"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$shell_file" >/dev/null
else
  sed -i 's/\r$//' "$shell_file"
fi
chown root:root "$project/app/sync_feishu_public.py" "$shell_file"
chmod 644 "$project/app/sync_feishu_public.py"
chmod 755 "$shell_file"

cd "$project"
.venv/bin/python -m py_compile app/sync_feishu_public.py
.venv/bin/python - <<'PY'
from app.sync_feishu_public import diff_public_records, gongkao_key

old = [{
    "record_id": "rec-stale",
    "fields": {
        "链接": {"link": "https://hera-webapp.fenbi.com/api/website/article/detail?id=1"},
        "来源": "自动·网站-粉笔",
    },
}]
assert diff_public_records([], old, gongkao_key, source_field="来源")[2] == ["rec-stale"]

manual = [{
    "record_id": "rec-manual",
    "fields": {"链接": {"link": "https://example.com/manual"}, "来源": "手动"},
}]
assert diff_public_records([], manual, gongkao_key, source_field="来源")[2] == []
print("Labeled automatic-row cleanup guard passed; manual rows remain protected")
PY

trap - EXIT
echo "Installed commit 00447ba stale labeled automatic-row cleanup"
echo "Run: cd /opt/hot-gap-aggregator && .venv/bin/python -m app.sync_feishu_public"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
