#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-daily-public-volume.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-daily-public-volume"
files=(
  app/daily_volume.py
  app/notify_gongkao_digest.py
  deploy/server/install-hot-gap-daily-public-volume.sh
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
    chmod 755 "$project/deploy/server/install-hot-gap-daily-public-volume.sh" 2>/dev/null || true
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project" "${files[@]}"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$project/deploy/server/install-hot-gap-daily-public-volume.sh" >/dev/null
else
  sed -i 's/\r$//' "$project/deploy/server/install-hot-gap-daily-public-volume.sh"
fi
chown root:root "$project/app/daily_volume.py" \
  "$project/app/notify_gongkao_digest.py" \
  "$project/deploy/server/install-hot-gap-daily-public-volume.sh"
chmod 644 "$project/app/daily_volume.py" "$project/app/notify_gongkao_digest.py"
chmod 755 "$project/deploy/server/install-hot-gap-daily-public-volume.sh"

cd "$project"
.venv/bin/python -m py_compile app/daily_volume.py app/notify_gongkao_digest.py
.venv/bin/python - <<'PY'
import json
from datetime import datetime, timedelta
from pathlib import Path

from app.daily_volume import CHINA_TZ, _public_table_volume, _read_items

today = datetime.now(CHINA_TZ).date()
report_date = today - timedelta(days=1)
data_dir = Path("/var/www/hot-gap/data")
result = _public_table_volume(
    _read_items(data_dir / "gongkao_enriched.json"),
    _read_items(data_dir / "qiuzhao.json"),
    report_date=report_date,
    today=today,
)
assert sum(result["gongkao_source_new"].values()) == result["gongkao_new"]
assert sum(result["qiuzhao_source_new"].values()) == result["qiuzhao_new"]
print(json.dumps({
    "report_date": report_date.isoformat(),
    "gongkao_public_candidates": result["gongkao_new"],
    "gongkao_sources": result["gongkao_source_new"],
    "gongkao_stages": result["gongkao_public_stages"],
    "qiuzhao_public_candidates": result["qiuzhao_new"],
    "qiuzhao_sources": result["qiuzhao_source_new"],
    "qiuzhao_stages": result["qiuzhao_public_stages"],
}, ensure_ascii=False))
PY

trap - EXIT
echo "Installed public-table-candidate daily reporting and corrected digest date label"
echo "Existing 07:00 daily cron and refresh schedules were not changed"
echo "No report, refresh, service, or container was started"
echo "Backup: $backup"
