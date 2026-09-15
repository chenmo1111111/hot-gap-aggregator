#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-fenbi-links-f569ebc.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-fenbi-links-f569ebc"
files=(
  app/collectors/gongkao.py
  app/pipeline/gongkao_links.py
  app/sync_feishu.py
  app/sync_feishu_public.py
  config/feishu_public_sync.yaml
  deploy/server/install-hot-gap-fenbi-links-f569ebc.sh
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
shell_file="$project/deploy/server/install-hot-gap-fenbi-links-f569ebc.sh"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$shell_file" >/dev/null
else
  sed -i 's/\r$//' "$shell_file"
fi
for relative in "${files[@]}"; do
  chown root:root "$project/$relative"
done
chmod 644 \
  "$project/app/collectors/gongkao.py" \
  "$project/app/pipeline/gongkao_links.py" \
  "$project/app/sync_feishu.py" \
  "$project/app/sync_feishu_public.py" \
  "$project/config/feishu_public_sync.yaml"
chmod 755 "$shell_file"

cd "$project"
.venv/bin/python -m py_compile \
  app/collectors/gongkao.py \
  app/pipeline/gongkao_links.py \
  app/sync_feishu.py \
  app/sync_feishu_public.py
.venv/bin/python - <<'PY'
from app.collectors.gongkao import GongkaoCollector
from app.sync_feishu import map_gongkao
from app.sync_feishu_public import load_public_config, map_public_gongkao

row = {
    "title": "事业单位招聘公告",
    "url": (
        "https://hera-webapp.fenbi.com/api/website/article/detail?"
        "deviceType=3&id=469068057956352&app=web"
    ),
    "extra": {"id": "469068057956352", "source_site": "fenbi"},
}
assert "hera-webapp.fenbi.com/api/" not in map_gongkao(row)["公告链接"]["link"]
assert "hera-webapp.fenbi.com/api/" not in map_public_gongkao(row)["链接"]["link"]

parsed = GongkaoCollector.parse_articles({"data": {"articles": [{
    "id": 1,
    "title": "测试招聘公告",
    "announcementArticleInfoRet": {
        "sourceInfo": {"sourceUrl": "https://t.fenbi.com/s/00TEST"},
    },
}]}})[0]
assert parsed.url == "https://t.fenbi.com/s/00TEST"

config = load_public_config("config/feishu_public_sync.yaml")
assert config.get("instructions_table_enabled") is False, config
print("Fenbi browser-link and Feishu guide guards passed")
PY

trap - EXIT
echo "Installed f569ebc browser-safe Fenbi links and b259dce Feishu guide cleanup"
echo "Run /usr/local/sbin/hot-gap-feishu-refresh separately to rewrite existing Feishu links"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
