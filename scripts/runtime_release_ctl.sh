#!/usr/bin/env bash
set -euo pipefail

# This controller is intentionally never called by CI. It is a deployment
# primitive for a separately approved test-server change window.
usage() {
  echo "usage: $0 activate|rollback RELEASES_DIR CURRENT_LINK CONTENT_ID [EXPECTED_OLD_ID]" >&2
  exit 64
}

[[ $# -ge 4 ]] || usage
action=$1
releases_dir=$2
current_link=$3
content_id=$4
expected_old_id=${5:-}

[[ "$content_id" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid content id" >&2; exit 65; }
target="$releases_dir/$content_id"
[[ -d "$target" && -f "$target/MANIFEST.json" ]] || { echo "release missing" >&2; exit 66; }
[[ ! -L "$target" ]] || { echo "release may not be a symlink" >&2; exit 67; }

current_id=""
if [[ -L "$current_link" ]]; then
  current_target=$(readlink -f "$current_link")
  current_id=$(basename "$current_target")
fi
if [[ -n "$expected_old_id" && "$current_id" != "$expected_old_id" ]]; then
  echo "compare-and-swap failed: current=$current_id expected=$expected_old_id" >&2
  exit 68
fi

case "$action" in
  activate|rollback)
    next_link="${current_link}.next.$$"
    ln -s "$target" "$next_link"
    mv -Tf "$next_link" "$current_link"
    printf '%s\n' "$current_id"
    ;;
  *) usage ;;
esac
