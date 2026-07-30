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
cleanup() { rm -f -- "$partial"; }
trap cleanup EXIT

# GNU tar writes the archive only to stdout. It creates no remote files.
ssh "${ssh_options[@]}" "$remote" \
  "tar -C / -cf - \
    --exclude='*.env' --exclude='*.key' --exclude='*.pem' --exclude='*.crt' \
    --exclude='*.p12' --exclude='*.db' --exclude='*.db-*' --exclude='*.sqlite*' \
    --exclude='*.log' --exclude='*.pid' --exclude='stats.json' \
    --exclude='*/content_out' --exclude='*/content_out/*' \
    --exclude='*/uploads' --exclude='*/uploads/*' \
    --exclude='*/generated' --exclude='*/generated/*' \
    --exclude='*/logs' --exclude='*/logs/*' \
    --exclude='*/cache' --exclude='*/cache/*' \
    --exclude='*/__pycache__' --exclude='*/__pycache__/*' \
    --exclude='*/browser_data' --exclude='*/browser_data/*' \
    --exclude='*/user_data' --exclude='*/user_data/*' \
    home/ubuntu/content-api \
    home/ubuntu/auth-service \
    home/ubuntu/dl-service \
    var/www/huangquechuanmei \
    etc/systemd/system \
    etc/nginx" > "$partial"

[[ -s "$partial" ]] || {
  echo "BLOCKED: capture archive is empty" >&2
  exit 68
}
mv -- "$partial" "$output"
trap - EXIT
printf 'captured_host=%s archive=%s\n' "$remote_hostname" "$output"
