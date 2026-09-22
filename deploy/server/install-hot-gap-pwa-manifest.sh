#!/usr/bin/env bash
set -Eeuo pipefail

project=/opt/hot-gap-aggregator
archive=/home/deploy/hot-gap-pwa-manifest.tar.gz
static_root=/var/www/hot-gap
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$project/.deploy-backups/${stamp}-before-pwa-manifest"
source_files=(
  public/manifest.json
  public/sw.js
  web/index.html
)
static_files=(
  index.html
  manifest.json
  sw.js
  icon-192.png
  icon-512.png
  icon-maskable-512.png
)

[[ -d "$project" && -f "$archive" ]] || {
  echo "Missing project or $archive" >&2
  exit 1
}

/usr/bin/install -d -m 755 "$backup"
for relative in "${source_files[@]}"; do
  if [[ -f "$project/$relative" ]]; then
    /usr/bin/install -D -p "$project/$relative" "$backup/project/$relative"
  else
    /usr/bin/install -D -m 600 /dev/null "$backup/project/$relative.absent"
  fi
done
for relative in "${static_files[@]}"; do
  if [[ -f "$static_root/$relative" ]]; then
    /usr/bin/install -D -p "$static_root/$relative" "$backup/static/$relative"
  else
    /usr/bin/install -D -m 600 /dev/null "$backup/static/$relative.absent"
  fi
done

stage="$(mktemp -d /tmp/hot-gap-pwa.XXXXXX)"
installed=0
cleanup() {
  status=$?
  if (( installed == 0 )); then
    echo "Deployment failed; restoring $backup" >&2
    for relative in "${source_files[@]}"; do
      if [[ -f "$backup/project/$relative" ]]; then
        /usr/bin/install -D -o root -g root -m 644 \
          "$backup/project/$relative" "$project/$relative"
      elif [[ -f "$backup/project/$relative.absent" ]]; then
        /usr/bin/rm -f -- "$project/$relative"
      fi
    done
    for relative in "${static_files[@]}"; do
      if [[ -f "$backup/static/$relative" ]]; then
        /usr/bin/install -D -o root -g root -m 644 \
          "$backup/static/$relative" "$static_root/$relative"
      elif [[ -f "$backup/static/$relative.absent" ]]; then
        /usr/bin/rm -f -- "$static_root/$relative"
      fi
    done
  fi
  /usr/bin/rm -rf -- "$stage"
  exit "$status"
}
trap cleanup EXIT

/usr/bin/tar -xzf "$archive" -C "$stage"
for relative in "${source_files[@]}"; do
  source_file="$stage/hot-gap-aggregator/$relative"
  [[ -f "$source_file" ]] || { echo "Archive missing $relative" >&2; exit 1; }
  /usr/bin/install -D -o root -g root -m 644 "$source_file" "$project/$relative"
done
for relative in "${static_files[@]}"; do
  source_file="$stage/hot-gap-aggregator/web/dist/$relative"
  [[ -f "$source_file" ]] || { echo "Archive missing web/dist/$relative" >&2; exit 1; }
  /usr/bin/install -D -o root -g root -m 644 "$source_file" "$static_root/$relative"
done

manifest_type="$(/usr/bin/curl -sS -I https://hot.weixincuotiben.top/manifest.json \
  | /usr/bin/awk 'BEGIN{IGNORECASE=1} /^content-type:/{gsub("\r", ""); print $2; exit}')"
if [[ "$manifest_type" != "application/json" ]]; then
  echo "Unexpected manifest content type: ${manifest_type:-missing}" >&2
  exit 1
fi

installed=1
echo "Installed JSON PWA manifest for Samsung Internet standalone installation"
echo "Manifest content type: $manifest_type"
echo "No service, cron schedule, refresh process, database, or container was changed"
echo "Backup: $backup"
