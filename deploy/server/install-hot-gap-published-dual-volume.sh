#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-published-dual-volume.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-published-dual-volume"
files=(
  app/export_qiuzhao.py
  app/daily_volume.py
  app/sync_feishu_public.py
)

[[ -f "$archive" ]] || { echo "Missing $archive" >&2; exit 1; }
/usr/bin/install -d -m 755 "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    /usr/bin/install -D -m 644 "$project/$relative" "$backup/$relative"
  fi
done

stage="$(mktemp -d /tmp/hot-gap-published-dual-volume.XXXXXX)"
trap '/usr/bin/rm -rf -- "$stage"' EXIT
/usr/bin/tar -xzf "$archive" -C "$stage"
for relative in "${files[@]}"; do
  source_file="$stage/hot-gap-aggregator/$relative"
  [[ -f "$source_file" ]] || { echo "Archive missing $relative" >&2; exit 1; }
  /usr/bin/install -D -o root -g root -m 644 "$source_file" "$project/$relative"
done

cd "$project"
.venv/bin/python -m py_compile "${files[@]}"
.venv/bin/python - <<'PY'
from app.export_qiuzhao import normalize_jobs_payload
from app.sync_feishu_public import map_public_qiuzhao

row = normalize_jobs_payload({"items": [{
    "title": "测试岗位", "url": "https://example.com/job",
    "published_at": "2026-09-25",
    "extra": {"company": "测试公司", "source_label": "国家大学生就业服务平台"},
}]})["items"][0]
assert row["published_at"] == "2026-09-25"
assert row["published_source_label"] == "国家大学生就业服务平台"
assert map_public_qiuzhao(row)["日期"] == 1790265600000
print("Verified website published_at preservation and public date mapping")
PY

echo "Installed website published_at preservation and dual-basis daily reporting"
echo "No cron schedule, service, database, refresh process, or container was changed"
echo "Backup: $backup"
