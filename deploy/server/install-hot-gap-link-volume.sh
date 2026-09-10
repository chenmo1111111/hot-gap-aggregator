#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-link-volume.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-link-volume"
static_root=/var/www/hot-gap
files=(
  app/capture_monitor.py
  app/collect_xiaozhaoya.py
  app/collectors/gongkao.py
  app/collectors/haitou.py
  app/collectors/nowcoder.py
  app/collectors/wutongguo.py
  app/collectors/yingjiesheng.py
  app/collectors/xiaozhaoya.py
  app/daily_volume.py
  app/export_gongkao.py
  app/export_qiuzhao.py
  app/notify_gongkao_digest.py
  app/pipeline/gongkao_filter.py
  app/pipeline/gov_link_resolver.py
  app/sync_feishu.py
  config/xiaozhaoya.yaml
  deploy/server/hot-gap-feishu-refresh
  deploy/server/install-hot-gap-link-volume.sh
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
sed -i 's/\r$//' \
  "$project/deploy/server/hot-gap-feishu-refresh" \
  "$project/deploy/server/install-hot-gap-link-volume.sh"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    chown root:root "$project/$relative"
    chmod 644 "$project/$relative"
  fi
done
install -o root -g root -m 755 \
  "$project/deploy/server/hot-gap-feishu-refresh" \
  /usr/local/sbin/hot-gap-feishu-refresh

cd "$project"
.venv/bin/python -m py_compile \
  app/capture_monitor.py app/collect_xiaozhaoya.py app/collectors/gongkao.py \
  app/collectors/xiaozhaoya.py app/daily_volume.py \
  app/export_gongkao.py app/export_qiuzhao.py \
  app/notify_gongkao_digest.py app/pipeline/gongkao_filter.py \
  app/pipeline/gov_link_resolver.py app/sync_feishu.py

if [[ -f "$project/web/dist/index.html" ]]; then
  install -o root -g root -m 644 "$project/web/dist/index.html" "$static_root/index.html"
  while IFS= read -r -d '' asset; do
    relative="${asset#"$project/web/dist/"}"
    install -D -o root -g root -m 644 "$asset" "$static_root/$relative"
  done < <(find "$project/web/dist/assets" -type f -print0)
fi

echo "Installed link policy, volume monitor, Xiaozhaoya preview and frontend"
echo "Backup: $backup"
echo "Running one protected refresh now"
/usr/local/sbin/hot-gap-feishu-refresh

trap - EXIT
echo "Deployment and refresh completed"
