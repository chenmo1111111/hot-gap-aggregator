#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-purchased-routing.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-purchased-routing"
files=(
  app/capture_gongkao_sheet.py
  app/capture_wanqing.py
  app/collectors/xiaozhaoya.py
  app/daily_volume.py
  app/export_gongkao.py
  app/export_qiuzhao.py
  app/pipeline/gongkao_classify.py
  app/pipeline/gongkao_enrich.py
  app/pipeline/purchased_classify.py
  app/sync_feishu.py
  config/gongkao_sheet_capture.yaml
  config/wanqing_capture.yaml
  deploy/server/install-hot-gap-purchased-routing.sh
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
shell_files=("$project/deploy/server/install-hot-gap-purchased-routing.sh")
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "${shell_files[@]}"
else
  sed -i 's/\r$//' "${shell_files[@]}"
fi
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    chown root:root "$project/$relative"
    chmod 644 "$project/$relative"
  fi
done

cd "$project"
.venv/bin/python -m py_compile \
  app/capture_gongkao_sheet.py app/capture_wanqing.py \
  app/collectors/xiaozhaoya.py app/daily_volume.py \
  app/export_gongkao.py app/export_qiuzhao.py \
  app/pipeline/gongkao_classify.py app/pipeline/gongkao_enrich.py \
  app/pipeline/purchased_classify.py app/sync_feishu.py

echo "Installed purchased-table sheet, routing, and daily-volume fixes"
echo "No cron schedule, service, or container was changed"
echo "Backup: $backup"
echo "Running one protected refresh now"
/usr/local/sbin/hot-gap-feishu-refresh

trap - EXIT
echo "Deployment and refresh completed"
