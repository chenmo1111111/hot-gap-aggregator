#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-gongkao-dedup.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-gongkao-dedup"
files=(
  app/export_gongkao.py
  app/pipeline/gongkao_dedup.py
  app/sync_feishu.py
  app/sync_feishu_public.py
  deploy/server/install-hot-gap-gongkao-dedup.sh
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
shell_file="$project/deploy/server/install-hot-gap-gongkao-dedup.sh"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$shell_file"
else
  sed -i 's/\r$//' "$shell_file"
fi
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    chown root:root "$project/$relative"
    chmod 644 "$project/$relative"
  fi
done

cd "$project"
.venv/bin/python -m py_compile \
  app/export_gongkao.py \
  app/pipeline/gongkao_dedup.py \
  app/sync_feishu.py \
  app/sync_feishu_public.py
.venv/bin/python - <<'PY'
from app.pipeline.gongkao_dedup import title_similarity

assert title_similarity(
    "2026年平原县事业单位引进优秀青年人才公告",
    "2026年德州平原县事业单位引进优秀青年人才公告（19名）",
) >= 0.9
assert title_similarity(
    "黑龙江省科学院微生物研究所2026年度公开招聘博士专业人员公告",
    "2026年黑龙江省科学院智能制造研究所公开招聘博士专业人员5人公告",
) < 0.7
PY

trap - EXIT
echo "Installed cross-source Gongkao deduplication and Feishu review fields"
echo "Runtime Feishu tokens/config values were preserved"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
echo "Refresh was not auto-started; run /usr/local/sbin/hot-gap-feishu-refresh separately"
