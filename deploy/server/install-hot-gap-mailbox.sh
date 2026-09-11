#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-mailbox-private.tar.gz
incoming=/home/deploy/.mailbox.env.incoming
static_root=/var/www/hot-gap
cron_target=/etc/cron.d/hot-gap-mailbox
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-private-mailbox"
files=(
  .env.example
  app/mailbox/__init__.py
  app/mailbox/__main__.py
  app/mailbox/extract.py
  app/mailbox/imap_client.py
  app/mailbox/store.py
  sync/app.py
  web/src/App.tsx
  deploy/server/hot-gap-mailbox.cron
  deploy/server/install-hot-gap-mailbox.sh
)

if [[ ! -d "$project" || ! -x "$project/.venv/bin/python" || ! -f "$archive" || ! -f "$incoming" ]]; then
  echo "Missing project, virtualenv Python, archive, or private mailbox environment input" >&2
  exit 1
fi

mkdir -p "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    install -D -p "$project/$relative" "$backup/$relative"
  fi
done
if [[ -f "$project/.env" ]]; then
  install -p -m 600 "$project/.env" "$backup/.env"
fi
if [[ -f "$cron_target" ]]; then
  install -p "$cron_target" "$backup/hot-gap-mailbox.cron"
else
  : > "$backup/cron-was-absent"
fi
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
    if [[ -f "$backup/.env" ]]; then
      install -o root -g root -m 600 "$backup/.env" "$project/.env"
    fi
    if [[ -f "$backup/hot-gap-mailbox.cron" ]]; then
      install -o root -g root -m 644 "$backup/hot-gap-mailbox.cron" "$cron_target"
    elif [[ -f "$backup/cron-was-absent" ]]; then
      rm -f -- "$cron_target"
    fi
    if [[ -f "$backup/static/index.html" ]]; then
      install -o root -g root -m 644 "$backup/static/index.html" "$static_root/index.html"
    fi
    systemctl restart hot-gap-sync.service >/dev/null 2>&1 || true
    systemctl restart cron.service >/dev/null 2>&1 || true
  fi
  rm -f -- "$incoming"
  exit "$status"
}
trap rollback EXIT

tar -xzf "$archive" -C "$project"
shell_files=(
  "$project/deploy/server/hot-gap-mailbox.cron"
  "$project/deploy/server/install-hot-gap-mailbox.sh"
)
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "${shell_files[@]}"
else
  sed -i 's/\r$//' "${shell_files[@]}"
fi

"$project/.venv/bin/python" - "$project/.env" "$incoming" <<'PY'
from pathlib import Path
import os
import sys

env_path, incoming_path = map(Path, sys.argv[1:3])
required = ("MAIL_IMAP_HOST", "MAIL_IMAP_PORT", "MAIL_IMAP_USER", "MAIL_IMAP_AUTH_CODE")
incoming = {}
for line in incoming_path.read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key in required:
        incoming[key] = value.strip()
if any(not incoming.get(key) for key in required):
    raise SystemExit("private mailbox environment input is incomplete")
try:
    port = int(incoming["MAIL_IMAP_PORT"])
except ValueError as exc:
    raise SystemExit("MAIL_IMAP_PORT must be an integer") from exc
if not 1 <= port <= 65535 or any(char.isspace() for char in incoming["MAIL_IMAP_HOST"]):
    raise SystemExit("mailbox host/port is invalid")
incoming["MAIL_DEADLINES_DB"] = "/var/lib/hot-gap-sync/mail-deadlines.db"

managed = set(required) | {"MAIL_DEADLINES_DB"}
lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
updated = []
written = set()
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in managed:
        if key not in written:
            updated.append(f"{key}={incoming[key]}")
            written.add(key)
        continue
    updated.append(line)
for key in (*required, "MAIL_DEADLINES_DB"):
    if key not in written:
        if updated and updated[-1]:
            updated.append("")
        updated.append(f"{key}={incoming[key]}")
        written.add(key)
temporary = env_path.with_name(f".{env_path.name}.mailbox.tmp")
temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, env_path)
PY
chown root:root "$project/.env"
chmod 600 "$project/.env"
rm -f -- "$incoming"

for relative in "${files[@]}"; do
  chown root:root "$project/$relative"
  chmod 644 "$project/$relative"
done
chmod 755 "$project/deploy/server/install-hot-gap-mailbox.sh"
install -d -o www-data -g www-data -m 750 /var/lib/hot-gap-sync

cd "$project"
.venv/bin/python -m py_compile app/mailbox/*.py sync/app.py
install -o root -g root -m 644 deploy/server/hot-gap-mailbox.cron "$cron_target"

if [[ -f "$project/web/dist/index.html" ]]; then
  install -o root -g root -m 644 "$project/web/dist/index.html" "$static_root/index.html"
  while IFS= read -r -d '' asset; do
    relative="${asset#"$project/web/dist/"}"
    install -D -o root -g root -m 644 "$asset" "$static_root/$relative"
  done < <(find "$project/web/dist/assets" -type f -print0)
else
  echo "Missing prebuilt web/dist" >&2
  exit 1
fi

systemctl restart hot-gap-sync.service
systemctl is-active --quiet hot-gap-sync.service
systemctl restart cron.service

trap - EXIT
echo "Installed private admin mailbox deadline reminders"
echo "Installed independent 20-minute cron; existing refresh cron was not changed"
echo "Mailbox credentials were written only to the root-only S1 .env"
echo "No collector, refresh, or container was started"
echo "Backup: $backup"
