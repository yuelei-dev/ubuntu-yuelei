#!/bin/sh
set -eu

LAUNCHER=/usr/local/sbin/huangque-release-test-initialize-ancestor-v1
ENTRYPOINT=/usr/local/libexec/huangque-release/test_release_ancestor_initializer_v1.py
EXPECTED_ENTRYPOINT_SHA256=fc28aadf3692c000fd683c7c59f0c52e87823534904f9846deeb4a3777c7d2a0

fail() {
    /usr/bin/printf '%s\n' "huangque ancestor initializer launcher: $1" >&2
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
    || fail "untrusted initializer"

actual_sha256=$(/usr/bin/sha256sum -- "$ENTRYPOINT")
actual_sha256=${actual_sha256%% *}
[ "$actual_sha256" = "$EXPECTED_ENTRYPOINT_SHA256" ] || fail "initializer hash mismatch"

exec /usr/bin/env -i \
    HOME=/root \
    PATH=/usr/bin:/bin \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    /usr/bin/python3 -I -E -s -B "$ENTRYPOINT" "$@"
