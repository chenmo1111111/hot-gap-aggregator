#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-gongkao-freshness.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-gongkao-freshness"
files=(
  app/collectors/base.py
  app/collectors/gongkao.py
  app/collectors/gongkao_types.py
  app/pipeline/gongkao_enrich.py
  app/pipeline/prune.py
  app/sync_feishu_public.py
  config/gongkao_fallback_sources.yaml
  config/retention.yaml
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
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    chown root:root "$project/$relative"
    chmod 644 "$project/$relative"
  fi
done

cd "$project"
.venv/bin/python -m py_compile \
  app/collectors/base.py app/collectors/gongkao.py app/collectors/gongkao_types.py \
  app/pipeline/gongkao_enrich.py app/pipeline/prune.py app/sync_feishu_public.py

echo "Installed Gongkao freshness fix"
echo "Backup: $backup"
echo "Running one protected Feishu refresh now"
/usr/local/sbin/hot-gap-feishu-refresh

trap - EXIT
echo "Deployment and refresh completed"
