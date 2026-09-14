#!/usr/bin/env bash
set -euo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-mailbox-prefilter.tar.gz
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-mailbox-prefilter"
files=(
  app/mailbox/imap_client.py
  deploy/server/install-hot-gap-mailbox-prefilter.sh
)

if [[ ! -d "$project" || ! -x "$project/.venv/bin/python" || ! -f "$archive" ]]; then
  echo "Missing project, virtualenv Python, or mailbox prefilter archive" >&2
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
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$project/deploy/server/install-hot-gap-mailbox-prefilter.sh" >/dev/null
else
  sed -i 's/\r$//' "$project/deploy/server/install-hot-gap-mailbox-prefilter.sh"
fi
chown root:root "$project/app/mailbox/imap_client.py" "$project/deploy/server/install-hot-gap-mailbox-prefilter.sh"
chmod 644 "$project/app/mailbox/imap_client.py"
chmod 755 "$project/deploy/server/install-hot-gap-mailbox-prefilter.sh"

cd "$project"
.venv/bin/python -m py_compile app/mailbox/imap_client.py
rescan_output="$(/usr/bin/timeout 14m /usr/bin/env MAILBOX_RUN_AS_USER=www-data .venv/bin/python -m app.mailbox --rescan-prefilter-misses)"
printf '%s\n' "$rescan_output"

trap - EXIT
echo "Installed mailbox initial/retest/interview prefilter coverage"
echo "Reprocessed matching prefilter misses from the last 30 days"
echo "No cron schedule, service, refresh process, or container was changed"
echo "Backup: $backup"
