#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-xiaozhaoya-foundation.tar.gz
private_env_incoming=/home/deploy/.xiaozhaoya-base-url.env.incoming
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-xiaozhaoya-foundation"
files=(
  app/capture_xiaozhaoya.py
  app/collectors/xiaozhaoya.py
  app/export_gongkao.py
  app/export_qiuzhao.py
  config/xiaozhaoya.yaml
  deploy/server/hot-gap-feishu-refresh
  deploy/server/install-hot-gap-xiaozhaoya-foundation.sh
)

if [[ ! -d "$project" || ! -f "$archive" || ! -f "$private_env_incoming" ]]; then
  echo "Missing project, deployment archive, or private Base URL" >&2
  exit 1
fi

mkdir -p "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    install -D -p "$project/$relative" "$backup/$relative"
  fi
done
if [[ -f "$project/.env" ]]; then
  install -p "$project/.env" "$backup/.env"
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
    if [[ -f "$backup/deploy/server/hot-gap-feishu-refresh" ]]; then
      install -o root -g root -m 755 \
        "$backup/deploy/server/hot-gap-feishu-refresh" \
        /usr/local/sbin/hot-gap-feishu-refresh
    fi
    if [[ -f "$backup/.env" ]]; then
      install -o root -g root -m 600 "$backup/.env" "$project/.env"
    fi
  fi
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project"
shell_files=(
  "$project/deploy/server/hot-gap-feishu-refresh"
  "$project/deploy/server/install-hot-gap-xiaozhaoya-foundation.sh"
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

"$project/.venv/bin/python" - "$project/.env" "$private_env_incoming" <<'PY'
from pathlib import Path
import os
import sys

env_path, incoming_path = map(Path, sys.argv[1:3])
key = "XIAOZHAOYA_FEISHU_BASE_URL"
incoming = incoming_path.read_text(encoding="utf-8").strip()
if not incoming.startswith(f"{key}=") or incoming == f"{key}=":
    raise SystemExit("private Base URL file is invalid")
lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
updated = []
replaced = False
for line in lines:
    if line.startswith(f"{key}="):
        if not replaced:
            updated.append(incoming)
            replaced = True
        continue
    updated.append(line)
if not replaced:
    if updated and updated[-1]:
        updated.append("")
    updated.append(incoming)
temporary = env_path.with_suffix(".env.tmp")
temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
os.replace(temporary, env_path)
PY
chown root:root "$project/.env"
chmod 600 "$project/.env"
rm -f -- "$private_env_incoming"

cd "$project"
.venv/bin/python -m py_compile \
  app/capture_xiaozhaoya.py app/collectors/xiaozhaoya.py \
  app/export_gongkao.py app/export_qiuzhao.py

echo "Installed Xiaozhaoya one-time foundation layer"
echo "Backup: $backup"
echo "Running one protected refresh now"
/usr/local/sbin/hot-gap-feishu-refresh

trap - EXIT
echo "Deployment and refresh completed"
