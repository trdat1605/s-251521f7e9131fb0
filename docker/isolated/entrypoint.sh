#!/bin/sh
set -eu

# The development image already contains gems at /gems. Do not install into
# a shared host bundle, delete another process's PID, or clear shared caches.
case "$1" in
  bin/vite|pnpm)
    if [ ! -x node_modules/.bin/vite ]; then
      echo "Frontend dependencies missing; build the development image before starting this project." >&2
      exit 1
    fi
    ;;
  *)
    bundle check
    ;;
esac
exec "$@"
