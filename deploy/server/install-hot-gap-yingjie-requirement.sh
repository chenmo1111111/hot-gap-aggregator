#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-yingjie-requirement.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-yingjie-requirement"
files=(
  app/pipeline/yingjie_requirement.py
  app/pipeline/gongkao_enrich.py
  app/sync_feishu_public.py
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
      elif [[ "$relative" == app/pipeline/yingjie_requirement.py ]]; then
        rm -f -- "$project/$relative"
      fi
    done
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project" "${files[@]}"
for relative in "${files[@]}"; do
  chown root:root "$project/$relative"
  chmod 644 "$project/$relative"
done
cd "$project"
.venv/bin/python -m py_compile "${files[@]}"
.venv/bin/python - <<'PY'
from app.pipeline.yingjie_requirement import classify_yingjie_requirement
from app.sync_feishu_public import GONGKAO_SCHEMA, map_public_gongkao

assert any(field["field_name"] == "应届要求" for field in GONGKAO_SCHEMA)
assert classify_yingjie_requirement({"title": "事业单位公开招聘公告"}) == "未明确"
assert map_public_gongkao({
    "title": "仅限应届毕业生招聘公告",
    "url": "https://example.com/notice",
})["应届要求"] == "仅应届"
PY

trap - EXIT
echo "Installed Gongkao public-table fresh-graduate requirement column"
echo "Next normal public-table sync will add and populate the select field"
echo "No refresh, cron schedule, service, or container was changed"
echo "Backup: $backup"
