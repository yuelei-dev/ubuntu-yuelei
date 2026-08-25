#!/bin/sh
set -eu

exec /usr/bin/env -i \
  HOME=/root \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PATH=/usr/bin:/bin \
  /usr/bin/python3 -I -E -s -B \
  /usr/local/libexec/huangque-release/test_release_transaction.py "$@"
