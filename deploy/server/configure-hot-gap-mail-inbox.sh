#!/usr/bin/env bash
set -euo pipefail

incoming=/home/deploy/.mail-inbox.env.incoming
installer=/home/deploy/install-hot-gap-mail-inbox.sh

if [[ "$(id -u)" -ne 0 || ! -f "$installer" ]]; then
  echo "Run this helper as root after uploading the installer" >&2
  exit 1
fi

read -r -p "QQ邮箱1账号: " qq1_user
read -r -s -p "QQ邮箱1 IMAP授权码（不回显）: " qq1_code
echo
read -r -p "QQ邮箱2账号: " qq2_user
read -r -s -p "QQ邮箱2 IMAP授权码（不回显）: " qq2_code
echo
read -r -p "163邮箱账号: " mail163_user
read -r -s -p "163邮箱 IMAP授权码（不回显）: " mail163_code
echo
read -r -p "Gmail 1账号: " gmail1_user
read -r -p "Gmail 2账号: " gmail2_user
read -r -p "Gmail 3账号: " gmail3_user
read -r -p "Google OAuth Client ID: " google_client_id
read -r -s -p "Google OAuth Client Secret（不回显）: " google_client_secret
echo

install -o root -g root -m 600 /dev/null "$incoming"
{
  printf '%s\n' \
    'MAIL_ACCOUNT_1_TYPE=imap' 'MAIL_ACCOUNT_1_LABEL=QQ邮箱1' \
    'MAIL_ACCOUNT_1_HOST=imap.qq.com' 'MAIL_ACCOUNT_1_PORT=993'
  printf 'MAIL_ACCOUNT_1_USER=%s\nMAIL_ACCOUNT_1_AUTH_CODE=%s\n' "$qq1_user" "$qq1_code"
  printf '%s\n' \
    'MAIL_ACCOUNT_2_TYPE=imap' 'MAIL_ACCOUNT_2_LABEL=QQ邮箱2' \
    'MAIL_ACCOUNT_2_HOST=imap.qq.com' 'MAIL_ACCOUNT_2_PORT=993'
  printf 'MAIL_ACCOUNT_2_USER=%s\nMAIL_ACCOUNT_2_AUTH_CODE=%s\n' "$qq2_user" "$qq2_code"
  printf '%s\n' \
    'MAIL_ACCOUNT_3_TYPE=imap' 'MAIL_ACCOUNT_3_LABEL=163邮箱' \
    'MAIL_ACCOUNT_3_HOST=imap.163.com' 'MAIL_ACCOUNT_3_PORT=993'
  printf 'MAIL_ACCOUNT_3_USER=%s\nMAIL_ACCOUNT_3_AUTH_CODE=%s\n' "$mail163_user" "$mail163_code"
  printf '%s\n' 'MAIL_ACCOUNT_4_TYPE=gmail' 'MAIL_ACCOUNT_4_LABEL=Gmail 1'
  printf 'MAIL_ACCOUNT_4_USER=%s\n' "$gmail1_user"
  printf '%s\n' 'MAIL_ACCOUNT_5_TYPE=gmail' 'MAIL_ACCOUNT_5_LABEL=Gmail 2'
  printf 'MAIL_ACCOUNT_5_USER=%s\n' "$gmail2_user"
  printf '%s\n' 'MAIL_ACCOUNT_6_TYPE=gmail' 'MAIL_ACCOUNT_6_LABEL=Gmail 3'
  printf 'MAIL_ACCOUNT_6_USER=%s\n' "$gmail3_user"
  printf 'MAIL_GOOGLE_CLIENT_ID=%s\nMAIL_GOOGLE_CLIENT_SECRET=%s\n' "$google_client_id" "$google_client_secret"
  printf '%s\n' 'MAIL_GOOGLE_REDIRECT_URI=https://hot.weixincuotiben.top/api/admin/mail-inbox/oauth/google/callback'
} > "$incoming"
chmod 600 "$incoming"

bash "$installer"
