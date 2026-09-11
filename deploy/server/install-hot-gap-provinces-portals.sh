#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-provinces-portals.tar.gz
static_root=/var/www/hot-gap
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-provinces-portals"
files=(
  app/collectors/gongkao.py
  app/collectors/gov_list.py
  app/pipeline/gongkao_filter.py
  app/recruitment_portals.py
  app/store/exporter.py
  config/gongkao_gov_sources.yaml
  config/recruitment_portals.yaml
  deploy/server/hot-gap-feishu-refresh
  deploy/server/install-hot-gap-provinces-portals.sh
  public/data/portals.json
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
if [[ -f "$static_root/index.html" ]]; then
  install -D -p "$static_root/index.html" "$backup/static/index.html"
fi
if [[ -f "$static_root/data/portals.json" ]]; then
  install -D -p "$static_root/data/portals.json" "$backup/static/data/portals.json"
fi

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      fi
    done
    if [[ -f "$backup/static/index.html" ]]; then
      install -o root -g root -m 644 "$backup/static/index.html" "$static_root/index.html"
    fi
    if [[ -f "$backup/static/data/portals.json" ]]; then
      install -D -o root -g root -m 644 \
        "$backup/static/data/portals.json" \
        "$static_root/data/portals.json"
    fi
    if [[ -f "$backup/deploy/server/hot-gap-feishu-refresh" ]]; then
      install -o root -g root -m 755 \
        "$backup/deploy/server/hot-gap-feishu-refresh" \
        /usr/local/sbin/hot-gap-feishu-refresh
    fi
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project"
shell_files=(
  "$project/deploy/server/hot-gap-feishu-refresh"
  "$project/deploy/server/install-hot-gap-provinces-portals.sh"
)
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "${shell_files[@]}"
else
  sed -i 's/\r$//' "${shell_files[@]}"
fi

for relative in "${files[@]}"; do
  chown root:root "$project/$relative"
  chmod 644 "$project/$relative"
done
install -o root -g root -m 755 \
  "$project/deploy/server/hot-gap-feishu-refresh" \
  /usr/local/sbin/hot-gap-feishu-refresh

cd "$project"
.venv/bin/python -m py_compile \
  app/collectors/gongkao.py app/collectors/gov_list.py \
  app/pipeline/gongkao_filter.py app/recruitment_portals.py app/store/exporter.py
.venv/bin/python -m app.recruitment_portals \
  --output "$static_root/data/portals.json"

if [[ -f "$project/web/dist/index.html" ]]; then
  install -o root -g root -m 644 "$project/web/dist/index.html" "$static_root/index.html"
  while IFS= read -r -d '' asset; do
    relative="${asset#"$project/web/dist/"}"
    install -D -o root -g root -m 644 "$asset" "$static_root/$relative"
  done < <(find "$project/web/dist/assets" -type f -print0)
fi

echo "Installed provincial recruitment sources and official portal navigation"
echo "Backup: $backup"
echo "Running one protected refresh now"
/usr/local/sbin/hot-gap-feishu-refresh

trap - EXIT
echo "Deployment and refresh completed"
