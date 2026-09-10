#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-gongkao-government.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-gongkao-government"
cron_backup="/root/crontab.before-hot-gap-government-${stamp}"
files=(
  app/collect_gongkao.py
  app/collectors/gongkao.py
  app/collectors/gov_list.py
  app/export_gongkao.py
  app/pipeline/gongkao_enrich.py
  app/pipeline/gongkao_filter.py
  app/pipeline/prune.py
  app/sync_feishu.py
  app/sync_feishu_public.py
  config/gongkao_gov_sources.yaml
  config/retention.yaml
  deploy/server/hot-gap-feishu-refresh
  DEPLOY_FEISHU_SYNC.md
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
crontab -l > "$cron_backup" 2>/dev/null || true

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      fi
    done
    if [[ -s "$cron_backup" ]]; then
      crontab "$cron_backup"
    fi
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project"
sed -i 's/\r$//' \
  "$project/deploy/server/hot-gap-feishu-refresh" \
  "$project/deploy/server/install-hot-gap-gongkao-government.sh"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    chown root:root "$project/$relative"
    chmod 644 "$project/$relative"
  fi
done
install -o root -g root -m 755 \
  "$project/deploy/server/hot-gap-feishu-refresh" \
  /usr/local/sbin/hot-gap-feishu-refresh

cron_tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -v '/usr/local/sbin/hot-gap-feishu-refresh' > "$cron_tmp" || true
echo '0 */3 * * * /usr/local/sbin/hot-gap-feishu-refresh >> /var/log/hot-gap-feishu-sync.log 2>&1' >> "$cron_tmp"
crontab "$cron_tmp"
rm -f -- "$cron_tmp"

cd "$project"
.venv/bin/python -m py_compile \
  app/collect_gongkao.py app/collectors/gongkao.py app/collectors/gov_list.py \
  app/pipeline/gongkao_filter.py app/pipeline/gongkao_enrich.py \
  app/pipeline/prune.py app/sync_feishu.py app/sync_feishu_public.py

echo "Installed government-first Gongkao collection"
echo "Backup: $backup"
echo "Cron backup: $cron_backup"
echo "Running one protected collection and Feishu refresh now"
/usr/local/sbin/hot-gap-feishu-refresh

trap - EXIT
echo "Deployment, refresh, and 3-hour timer installation completed"
