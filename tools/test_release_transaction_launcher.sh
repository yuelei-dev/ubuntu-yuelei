#!/bin/sh
set -eu

LAUNCHER=/usr/local/sbin/huangque-release-test-transaction
ENTRYPOINT=/usr/local/libexec/huangque-release/test_release_transaction.py
EXPECTED_ENTRYPOINT_SHA256=b4a6a9b4bd1d368dbd33d30588a4cee151c9446f6fef172c4fca2881e82fba61

fail() {
  /usr/bin/printf '%s\n' "huangque transaction launcher: $1" >&2
  exit 126
}

[ "$(/usr/bin/id -u)" = "0" ] || fail "root is required"
[ "$(/usr/bin/readlink -f -- "$0")" = "$LAUNCHER" ] || fail "non-canonical launcher path"

for directory in / /usr /usr/local /usr/local/sbin /usr/local/libexec /usr/local/libexec/huangque-release; do
  [ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$directory")" = "0:0:755:directory" ] \
    || fail "untrusted parent directory: $directory"
done

[ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$LAUNCHER")" = "0:0:755:regular file" ] \
  || fail "untrusted launcher"
[ "$(/usr/bin/stat -c '%u:%g:%a:%F' -- "$ENTRYPOINT")" = "0:0:755:regular file" ] \
  || fail "untrusted transaction entrypoint"

actual_sha256=$(/usr/bin/sha256sum -- "$ENTRYPOINT")
actual_sha256=${actual_sha256%% *}
[ "$actual_sha256" = "$EXPECTED_ENTRYPOINT_SHA256" ] \
  || fail "transaction entrypoint hash mismatch"

exec /usr/bin/env -i \
  HOME=/root \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PATH=/usr/bin:/bin \
  /usr/bin/python3 -I -E -s -B \
  "$ENTRYPOINT" "$@"
