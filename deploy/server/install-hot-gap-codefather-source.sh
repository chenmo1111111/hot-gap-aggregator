#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-codefather-source.tar.gz
data_dir=/var/www/hot-gap/data
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-codefather-source"
files=(
  .env.example
  app/capture_codefather.py
  app/daily_volume.py
  app/export_qiuzhao.py
  app/pipeline/qiuzhao_dedup.py
  deploy/server/hot-gap-feishu-refresh
)

[[ -f "$archive" ]] || { echo "Missing $archive" >&2; exit 1; }
/usr/bin/install -d -m 755 "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    /usr/bin/install -D -m 644 "$project/$relative" "$backup/$relative"
  fi
done
for filename in qiuzhao.json qiuzhao_codefather.json; do
  if [[ -f "$data_dir/$filename" ]]; then
    /usr/bin/install -D -m 644 "$data_dir/$filename" "$backup/data/$filename"
  fi
done

stage="$(mktemp -d /tmp/hot-gap-codefather.XXXXXX)"
installed=0
cleanup() {
  if (( installed == 0 )); then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        /usr/bin/install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      else
        /usr/bin/rm -f -- "$project/$relative"
      fi
    done
    if [[ -f "$backup/deploy/server/hot-gap-feishu-refresh" ]]; then
      /usr/bin/install -o root -g root -m 755 \
        "$backup/deploy/server/hot-gap-feishu-refresh" \
        /usr/local/sbin/hot-gap-feishu-refresh
    fi
    for filename in qiuzhao.json qiuzhao_codefather.json; do
      if [[ -f "$backup/data/$filename" ]]; then
        /usr/bin/install -D -o deploy -g deploy -m 664 "$backup/data/$filename" "$data_dir/$filename"
      elif [[ "$filename" == "qiuzhao_codefather.json" ]]; then
        /usr/bin/rm -f -- "$data_dir/$filename"
      fi
    done
  fi
  /usr/bin/rm -rf -- "$stage"
}
trap cleanup EXIT

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
/bin/chmod 755 "$project/deploy/server/hot-gap-feishu-refresh"

cd "$project"
.venv/bin/python -m py_compile \
  app/capture_codefather.py app/daily_volume.py app/export_qiuzhao.py \
  app/pipeline/qiuzhao_dedup.py
/bin/bash -n deploy/server/hot-gap-feishu-refresh

.venv/bin/python -m app.capture_codefather --force
.venv/bin/python -m app.export_qiuzhao
.venv/bin/python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/var/www/hot-gap/data/qiuzhao.json").read_text(encoding="utf-8"))
status = payload.get("status") or {}
print(json.dumps({
    "event": "codefather_deploy_validation",
    "codefather_input": status.get("codefather_input_count", 0),
    "codefather_merged": status.get("codefather_merged_count", 0),
    "codefather_new": status.get("codefather_new_count", 0),
    "qiuzhao_output": len(payload.get("items") or []),
}, ensure_ascii=False))
PY
/bin/chown deploy:deploy "$data_dir/qiuzhao.json" "$data_dir/qiuzhao_codefather.json"
/bin/chmod 664 "$data_dir/qiuzhao.json" "$data_dir/qiuzhao_codefather.json"

installed=1
echo "Installed Codefather recent-30-day Qiuzhao source and lossless cross-source enrichment"
echo "The source snapshot and qiuzhao.json were rebuilt; Feishu sync and refresh were not started"
echo "Existing cron schedules, services, and containers were not changed"
echo "Backup: $backup"
