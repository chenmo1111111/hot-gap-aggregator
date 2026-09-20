#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-shasha-source.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-shasha-source"
files=(
  app/capture_shasha.py
  app/daily_volume.py
  app/export_qiuzhao.py
  app/pipeline/qiuzhao_dedup.py
  app/sync_feishu.py
  app/sync_feishu_public.py
  config/shasha_capture.yaml
  deploy/server/hot-gap-feishu-refresh
)

[[ -f "$archive" ]] || { echo "Missing $archive" >&2; exit 1; }
/usr/bin/install -d -m 755 "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    /usr/bin/install -D -m 644 "$project/$relative" "$backup/$relative"
  fi
done

stage="$(mktemp -d /tmp/hot-gap-shasha.XXXXXX)"
trap '/usr/bin/rm -rf -- "$stage"' EXIT
/usr/bin/tar -xzf "$archive" -C "$stage"
for relative in "${files[@]}"; do
  source_file="$stage/hot-gap-aggregator/$relative"
  [[ -f "$source_file" ]] || { echo "Archive missing $relative" >&2; exit 1; }
  /usr/bin/install -D -o root -g root -m 644 "$source_file" "$project/$relative"
done

/usr/bin/dos2unix "$project/deploy/server/hot-gap-feishu-refresh" >/dev/null 2>&1 || true
/usr/bin/install -o root -g root -m 755 \
  "$project/deploy/server/hot-gap-feishu-refresh" \
  /usr/local/sbin/hot-gap-feishu-refresh
chmod 755 "$project/deploy/server/hot-gap-feishu-refresh"
cd "$project"
.venv/bin/python -m py_compile \
  app/capture_shasha.py app/daily_volume.py app/export_qiuzhao.py \
  app/pipeline/qiuzhao_dedup.py app/sync_feishu.py app/sync_feishu_public.py

echo "Installed Shasha purchased-source capture, lossless Qiuzhao dedup, filters, and refresh intake"
echo "No cron schedule, refresh process, service, or container was started"
echo "Backup: $backup"
