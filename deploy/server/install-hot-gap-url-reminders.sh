#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-url-reminders.tar.gz
static_root=/var/www/hot-gap
database=/var/lib/hot-gap-sync/mail-deadlines.db
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-url-reminders"
files=(
  app/mailbox/__main__.py
  app/mailbox/reminder_url.py
  app/mailbox/store.py
  requirements-server.txt
  sync/app.py
)

[[ -d "$project" && -x "$project/.venv/bin/python" && -f "$archive" ]] || {
  echo "Missing project, virtualenv Python, or $archive" >&2
  exit 1
}

/usr/bin/install -d -m 755 "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    /usr/bin/install -D -p "$project/$relative" "$backup/$relative"
  else
    /usr/bin/install -D -m 600 /dev/null "$backup/$relative.absent"
  fi
done
if [[ -f "$static_root/index.html" ]]; then
  /usr/bin/install -D -p "$static_root/index.html" "$backup/static/index.html"
fi
if [[ -d "$static_root/assets" ]]; then
  /bin/cp -a "$static_root/assets" "$backup/static/assets"
fi
if [[ -f "$database" ]]; then
  "$project/.venv/bin/python" - "$database" "$backup/mail-deadlines.db" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1:]
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
PY
  /bin/chmod 600 "$backup/mail-deadlines.db"
fi

stage="$(mktemp -d /tmp/hot-gap-url-reminders.XXXXXX)"
installed=0
cleanup() {
  status=$?
  if (( installed == 0 )); then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${files[@]}"; do
      if [[ -f "$backup/$relative" ]]; then
        /usr/bin/install -D -o root -g root -m 644 "$backup/$relative" "$project/$relative"
      elif [[ -f "$backup/$relative.absent" ]]; then
        /usr/bin/rm -f -- "$project/$relative"
      fi
    done
    if [[ -f "$backup/static/index.html" ]]; then
      /usr/bin/install -o root -g root -m 644 "$backup/static/index.html" "$static_root/index.html"
    fi
    if [[ -d "$backup/static/assets" ]]; then
      /usr/bin/install -d -o root -g root -m 755 "$static_root/assets"
      /bin/cp -a "$backup/static/assets/." "$static_root/assets/"
    fi
    /bin/systemctl restart hot-gap-sync.service >/dev/null 2>&1 || true
  fi
  /usr/bin/rm -rf -- "$stage"
  exit "$status"
}
trap cleanup EXIT

/usr/bin/tar -xzf "$archive" -C "$stage"
for relative in "${files[@]}"; do
  source_file="$stage/hot-gap-aggregator/$relative"
  [[ -f "$source_file" ]] || { echo "Archive missing $relative" >&2; exit 1; }
  /usr/bin/install -D -o root -g root -m 644 "$source_file" "$project/$relative"
done
[[ -f "$stage/hot-gap-aggregator/web/dist/index.html" ]] || {
  echo "Archive missing prebuilt web/dist" >&2
  exit 1
}

cd "$project"
.venv/bin/python -m pip install 'pypdf>=5,<7'
.venv/bin/python -m py_compile app/mailbox/*.py sync/app.py
/usr/bin/install -d -o www-data -g www-data -m 750 /var/lib/hot-gap-sync
/usr/sbin/runuser -u www-data -- .venv/bin/python -c \
  'from app.mailbox.store import MailboxStore; MailboxStore().initialize(); import sync.app'

/usr/bin/install -o root -g root -m 644 \
  "$stage/hot-gap-aggregator/web/dist/index.html" "$static_root/index.html"
while IFS= read -r -d '' asset; do
  relative="${asset#"$stage/hot-gap-aggregator/web/dist/"}"
  /usr/bin/install -D -o root -g root -m 644 "$asset" "$static_root/$relative"
done < <(/usr/bin/find "$stage/hot-gap-aggregator/web/dist/assets" -type f -print0)

/bin/systemctl restart hot-gap-sync.service
service_ready=0
for _ in {1..15}; do
  /bin/sleep 1
  if [[ "$(/bin/systemctl show hot-gap-sync.service --property=ActiveState --value)" == "active" \
    && "$(/bin/systemctl show hot-gap-sync.service --property=SubState --value)" == "running" ]]; then
    service_ready=1
    break
  fi
done
if [[ "$service_ready" -ne 1 ]]; then
  /bin/systemctl --no-pager --full status hot-gap-sync.service || true
  echo "hot-gap-sync.service did not become active" >&2
  exit 1
fi

installed=1
echo "Installed admin URL-to-reminder extraction with editable confirmation"
echo "Existing email reminders and the independent 20-minute cron were preserved"
echo "No public data pipeline, Feishu sync, refresh schedule, or container was changed"
echo "Backup: $backup"
