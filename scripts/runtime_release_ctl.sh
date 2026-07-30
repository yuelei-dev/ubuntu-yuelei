#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage:
  runtime_release_ctl.sh initialize RELEASES_DIR CURRENT_LINK CONTENT_ID
  runtime_release_ctl.sh activate|rollback RELEASES_DIR CURRENT_LINK CONTENT_ID EXPECTED_OLD_ID
EOF
  exit 64
}

[[ $# -ge 1 ]] || usage
action=$1
case "$action" in
  initialize) [[ $# -eq 4 ]] || usage ;;
  activate|rollback) [[ $# -eq 5 ]] || usage ;;
  *) usage ;;
esac

releases_dir=$2
current_link=$3
content_id=$4
expected_old_id=${5-}
id_pattern='^[0-9a-f]{64}$'
[[ "$content_id" =~ $id_pattern ]] || { echo "invalid content id" >&2; exit 65; }
if [[ "$action" != "initialize" ]]; then
  [[ "$expected_old_id" =~ $id_pattern ]] || { echo "invalid expected old id" >&2; exit 65; }
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
scope_file=$(realpath -e -- "$script_dir/../deploy/runtime-canonical/scope.json")
verifier=$(realpath -e -- "$script_dir/runtime_canonical.py")
releases_real=$(realpath -e -- "$releases_dir")
current_parent=$(realpath -e -- "$(dirname -- "$current_link")")
current_link="$current_parent/$(basename -- "$current_link")"
target=$(realpath -e -- "$releases_real/$content_id")

case "$target/" in
  "$releases_real/"*) ;;
  *) echo "release escapes releases directory" >&2; exit 66 ;;
esac
[[ -d "$target" && ! -L "$target" ]] || { echo "invalid release directory" >&2; exit 66; }

python3 "$verifier" \
  --scope "$scope_file" \
  verify-release \
  --release "$target" \
  --require-server-verified

exec 9>"${current_link}.lock"
flock -x 9

current_id=""
if [[ -e "$current_link" && ! -L "$current_link" ]]; then
  echo "current path exists but is not a symlink" >&2
  exit 67
fi
if [[ -L "$current_link" ]]; then
  current_target=$(realpath -e -- "$current_link")
  case "$current_target/" in
    "$releases_real/"*) ;;
    *) echo "current release escapes releases directory" >&2; exit 67 ;;
  esac
  current_id=$(basename -- "$current_target")
fi

if [[ "$action" == "initialize" ]]; then
  [[ -z "$current_id" ]] || { echo "current release already initialized" >&2; exit 68; }
else
  [[ "$current_id" == "$expected_old_id" ]] || {
    echo "compare-and-swap failed: current=$current_id expected=$expected_old_id" >&2
    exit 68
  }
fi

next_link="${current_link}.next.$$"
cleanup() { rm -f -- "$next_link"; }
trap cleanup EXIT
ln -s -- "$target" "$next_link"
mv -Tf -- "$next_link" "$current_link"
trap - EXIT
printf '%s\n' "$current_id"
