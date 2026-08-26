#!/bin/sh
set -eu

LAUNCHER=/usr/local/sbin/huangque-release-test
ENTRYPOINT=/usr/local/libexec/huangque-release/release_test.py
EXPECTED_ENTRYPOINT_SHA256=d740f5e1656caebf67a33ee52aa6c731433886290a7bf8badd8b307126492abb

fail() {
    /usr/bin/printf '%s\n' "huangque release launcher: $1" >&2
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
    || fail "untrusted entrypoint"

actual_sha256=$(/usr/bin/sha256sum -- "$ENTRYPOINT")
actual_sha256=${actual_sha256%% *}
[ "$actual_sha256" = "$EXPECTED_ENTRYPOINT_SHA256" ] \
    || fail "entrypoint hash mismatch"

exec /usr/bin/env -i \
    HOME=/root \
    PATH=/usr/bin:/bin \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    /usr/bin/python3 -I -E -s -B "$ENTRYPOINT" "$@"
