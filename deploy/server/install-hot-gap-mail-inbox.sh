#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-mail-inbox-private.tar.gz
incoming=/home/deploy/.mail-inbox.env.incoming
service_env=/etc/hot-gap-sync.env
static_root=/var/www/hot-gap
cron_target=/etc/cron.d/hot-gap-mail-inbox
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-private-mail-inbox"
files=(
  .env.example requirements.txt
  app/mailbox/accounts.py app/mailbox/classify.py app/mailbox/gmail_client.py
  app/mailbox/imap_client.py app/mailbox/inbox_store.py app/mailbox/inbox_sync.py
  sync/app.py web/src/App.tsx
  deploy/server/hot-gap-mail-inbox.cron
  deploy/server/install-hot-gap-mail-inbox.sh
  deploy/server/configure-hot-gap-mail-inbox.sh deploy/server/README.md
)

if [[ ! -d "$project" || ! -x "$project/.venv/bin/python" || ! -f "$archive" || ! -f "$incoming" ]]; then
  echo "Missing project, virtualenv Python, archive, or private inbox environment input" >&2
  exit 1
fi

mkdir -p "$backup"
for relative in "${files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    install -D -p "$project/$relative" "$backup/$relative"
  fi
done
for source in "$project/.env" "$service_env" "$cron_target"; do
  if [[ -f "$source" ]]; then
    install -D -p "$source" "$backup/system$source"
  fi
done
if [[ ! -f "$cron_target" ]]; then
  : > "$backup/cron-was-absent"
fi
if [[ -d "$project/web/dist" ]]; then
  cp -a "$project/web/dist" "$backup/web-dist"
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
    for target in "$project/.env" "$service_env" "$cron_target"; do
      if [[ -f "$backup/system$target" ]]; then
        install -D -o root -g root -m 600 "$backup/system$target" "$target"
      fi
    done
    if [[ -f "$backup/cron-was-absent" ]]; then
      rm -f -- "$cron_target"
    fi
    if [[ -f "$cron_target" ]]; then
      chmod 644 "$cron_target"
    fi
    if [[ -d "$backup/web-dist" ]]; then
      cp -a "$backup/web-dist/." "$project/web/dist/"
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
  "$project/deploy/server/hot-gap-mail-inbox.cron"
  "$project/deploy/server/install-hot-gap-mail-inbox.sh"
  "$project/deploy/server/configure-hot-gap-mail-inbox.sh"
)
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "${shell_files[@]}" >/dev/null
else
  sed -i 's/\r$//' "${shell_files[@]}"
fi

if ! "$project/.venv/bin/python" -c 'import cryptography' >/dev/null 2>&1; then
  "$project/.venv/bin/pip" install 'cryptography>=43,<51'
fi

encryption_key="$($project/.venv/bin/python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
"$project/.venv/bin/python" - "$project/.env" "$service_env" "$incoming" "$encryption_key" <<'PY'
from pathlib import Path
import os
import sys

project_env, service_env, incoming_path = map(Path, sys.argv[1:4])
generated_key = sys.argv[4]
required = {
    *(f"MAIL_ACCOUNT_{number}_{suffix}" for number in range(1, 4)
      for suffix in ("TYPE", "LABEL", "HOST", "PORT", "USER", "AUTH_CODE")),
    *(f"MAIL_ACCOUNT_{number}_{suffix}" for number in range(4, 7)
      for suffix in ("TYPE", "LABEL", "USER")),
    "MAIL_GOOGLE_CLIENT_ID", "MAIL_GOOGLE_CLIENT_SECRET", "MAIL_GOOGLE_REDIRECT_URI",
}
incoming = {}
for line in incoming_path.read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key in required or key == "MAIL_TOKEN_ENCRYPTION_KEY":
        incoming[key] = value.strip()
if any(not incoming.get(key) for key in required):
    missing = sorted(key for key in required if not incoming.get(key))
    raise SystemExit("private inbox environment input is incomplete: " + ", ".join(missing))
for number in range(1, 4):
    if incoming[f"MAIL_ACCOUNT_{number}_TYPE"] != "imap":
        raise SystemExit(f"MAIL_ACCOUNT_{number}_TYPE must be imap")
for number in range(4, 7):
    if incoming[f"MAIL_ACCOUNT_{number}_TYPE"] != "gmail":
        raise SystemExit(f"MAIL_ACCOUNT_{number}_TYPE must be gmail")
def existing_value(path: Path, wanted: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{wanted}="):
            return line.split("=", 1)[1].strip()
    return ""

existing_key = existing_value(project_env, "MAIL_TOKEN_ENCRYPTION_KEY") or existing_value(
    service_env, "MAIL_TOKEN_ENCRYPTION_KEY"
)
supplied_key = incoming.get("MAIL_TOKEN_ENCRYPTION_KEY", "")
if existing_key and supplied_key and existing_key != supplied_key:
    raise SystemExit("refusing to replace the existing Gmail token encryption key")
incoming["MAIL_TOKEN_ENCRYPTION_KEY"] = supplied_key or existing_key or generated_key
incoming["MAIL_INBOX_DB"] = "/var/lib/hot-gap-sync/mail-inbox.db"
incoming["MAIL_INBOX_RETENTION_DAYS"] = "90"
managed = set(incoming)

def update(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output = []
    written = set()
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in managed:
            if key not in written:
                output.append(f"{key}={incoming[key]}")
                written.add(key)
            continue
        output.append(line)
    for key in sorted(managed):
        if key not in written:
            output.append(f"{key}={incoming[key]}")
    temporary = path.with_name(f".{path.name}.mail-inbox.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

update(project_env)
update(service_env)
PY
chown root:root "$project/.env" "$service_env"
chmod 600 "$project/.env" "$service_env"
rm -f -- "$incoming"

for relative in "${files[@]}"; do
  chown root:root "$project/$relative"
  chmod 644 "$project/$relative"
done
chmod 755 "$project/app/mailbox" "$project/deploy/server/install-hot-gap-mail-inbox.sh" "$project/deploy/server/configure-hot-gap-mail-inbox.sh"
install -d -o www-data -g www-data -m 750 /var/lib/hot-gap-sync
touch /var/lib/hot-gap-sync/mail-inbox.db
chown www-data:www-data /var/lib/hot-gap-sync/mail-inbox.db
chmod 600 /var/lib/hot-gap-sync/mail-inbox.db

cd "$project"
.venv/bin/python -m py_compile app/mailbox/*.py sync/app.py
runuser -u www-data -- /usr/bin/env MAIL_INBOX_DB=/var/lib/hot-gap-sync/mail-inbox.db .venv/bin/python -c 'from app.mailbox.inbox_store import initialize_inbox_database; initialize_inbox_database()'
install -o root -g root -m 644 deploy/server/hot-gap-mail-inbox.cron "$cron_target"

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
for _ in {1..15}; do
  sleep 1
  if [[ "$(systemctl is-active hot-gap-sync.service)" == "active" ]]; then
    break
  fi
done
if [[ "$(systemctl is-active hot-gap-sync.service)" != "active" ]]; then
  systemctl --no-pager --full status hot-gap-sync.service || true
  exit 1
fi
systemctl restart cron.service

trap - EXIT
echo "Installed private six-account read-only inbox"
echo "Installed independent hourly cron at minute 17; deadline and refresh cron entries were not changed"
echo "Mailbox secrets were written only to root-owned environment files"
echo "No inbox sync, collector, refresh, or container was started"
echo "Backup: $backup"
