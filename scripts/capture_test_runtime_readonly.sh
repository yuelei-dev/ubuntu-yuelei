#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 SSH_USER@8.148.158.106 OUTPUT_TAR" >&2
  exit 64
}

[[ $# -eq 2 ]] || usage
remote=$1
output=$2
host=${remote##*@}

[[ "$host" == "8.148.158.106" ]] || {
  echo "BLOCKED: only the test server 8.148.158.106 may be captured" >&2
  exit 65
}
[[ "$host" != "129.204.166.13" ]] || {
  echo "BLOCKED: production capture is forbidden" >&2
  exit 65
}
[[ ! -e "$output" ]] || {
  echo "BLOCKED: output already exists: $output" >&2
  exit 66
}
config_dir="${output}.configs"
[[ ! -e "$config_dir" ]] || {
  echo "BLOCKED: config output already exists: $config_dir" >&2
  exit 66
}

ssh_options=(
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ClearAllForwardings=yes
  -o PermitLocalCommand=no
  -o RequestTTY=no
  -o StrictHostKeyChecking=yes
)

remote_hostname=$(ssh "${ssh_options[@]}" "$remote" 'hostname')
[[ -n "$remote_hostname" ]] || {
  echo "BLOCKED: remote hostname is empty" >&2
  exit 67
}

partial="${output}.partial.$$"
partial_configs="${config_dir}.partial.$$"
cleanup() {
  rm -f -- "$partial"
  rm -rf -- "$partial_configs"
}
trap cleanup EXIT
mkdir -- "$partial_configs"

# GNU tar writes the archive only to stdout. It creates no remote files.
ssh "${ssh_options[@]}" "$remote" \
  "tar -C / -cf - \
    --exclude='*.env' --exclude='*.key' --exclude='*.pem' --exclude='*.crt' \
    --exclude='*.p12' --exclude='*.db' --exclude='*.db-*' --exclude='*.sqlite*' \
    --exclude='*.log' --exclude='*.pid' --exclude='stats.json' \
    --exclude='*/content_out' --exclude='*/content_out/*' \
    --exclude='*/backups' --exclude='*/backups/*' \
    --exclude='*/uploads' --exclude='*/uploads/*' \
    --exclude='*/generated' --exclude='*/generated/*' \
    --exclude='*/logs' --exclude='*/logs/*' \
    --exclude='*/cache' --exclude='*/cache/*' \
    --exclude='*/data' --exclude='*/data/*' \
    --exclude='*/__pycache__' --exclude='*/__pycache__/*' \
    --exclude='*/browser_data' --exclude='*/browser_data/*' \
    --exclude='*/user_data' --exclude='*/user_data/*' \
    home/ubuntu/content-api \
    home/ubuntu/auth-service \
    home/ubuntu/dl-service \
    var/www/huangquechuanmei" > "$partial"

[[ -s "$partial" ]] || {
  echo "BLOCKED: capture archive is empty" >&2
  exit 68
}
units=(
  huangque-admin.service
  huangque-auth.service
  huangque-content.service
  huangque-dl.service
  huangque-egress-tunnel.service
  huangque-imggen-api.service
  huangque-leadgen-api.service
  huangque-repo-sync.service
  huangque-repo-sync.timer
)
for unit in "${units[@]}"; do
  ssh "${ssh_options[@]}" "$remote" \
    "systemctl cat -- '$unit' | sed -E \
      's/^[[:space:]]*Environment=.*/Environment=<redacted>/; \
       s/(--(token|password|api-key|secret)[ =])[^ ]+/\\1<redacted>/gI'" \
    > "$partial_configs/$unit"
  [[ -s "$partial_configs/$unit" ]] || {
    echo "BLOCKED: empty sanitized unit output: $unit" >&2
    exit 69
  }
done
ssh "${ssh_options[@]}" "$remote" \
  "nginx -T 2>&1 | sed -E \
    's/(--(token|password|api-key|secret)[ =])[^ ]+/\\1<redacted>/gI'" \
  > "$partial_configs/nginx-T.conf"
[[ -s "$partial_configs/nginx-T.conf" ]] || {
  echo "BLOCKED: empty nginx -T output" >&2
  exit 69
}

mv -- "$partial" "$output"
mv -- "$partial_configs" "$config_dir"
trap - EXIT
printf 'captured_host=%s archive=%s configs=%s\n' \
  "$remote_hostname" "$output" "$config_dir"
