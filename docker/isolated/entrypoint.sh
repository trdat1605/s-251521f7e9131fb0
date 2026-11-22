#!/bin/sh
set -eu

# The development image already contains gems at /gems. Do not install into
# a shared host bundle, delete another process's PID, or clear shared caches.
case "$1" in
  bin/vite|pnpm)
    pnpm install --frozen-lockfile
    ;;
  *)
    bundle check
    ;;
esac
exec "$@"

