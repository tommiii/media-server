#!/bin/bash
# Installs the daily job of this repository in YOUR crontab (no root needed). Idempotent.
#
#   ./scripts/cron.sh install                  daily at 04:30: remove duplicate downloads
#   ./scripts/cron.sh install --keep-manual    same, but never touch torrents you added by hand
#                                              (or set DEDUPE_ARGS=--keep-manual in .env, so setup.sh keeps it)
#   ./scripts/cron.sh remove                   take the job out again
#   ./scripts/cron.sh show                     print what is installed
#
# Log: $BASE_DIR/services/dedupe.log (see .env).
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD
[ -f .env ] && { set -a; . ./.env; set +a; }
LOG="${BASE_DIR:-$ROOT}/services/dedupe.log"
TAG="# media-server: dedupe"

current() { crontab -l 2>/dev/null || true; }
without_ours() { current | grep -v -F "$TAG"; }

case "${1:-show}" in
  install)
    shift
    mkdir -p "$(dirname "$LOG")"
    extra="${*:-${DEDUPE_ARGS:-}}"   # e.g. --keep-manual, from the command line or DEDUPE_ARGS in .env
    job="30 4 * * * cd '$ROOT' && ./scripts/dedupe_downloads.py --apply $extra >> '$LOG' 2>&1 $TAG"
    kept=$(without_ours)            # read first, write after: never read and rewrite at the same time
    { [ -n "$kept" ] && printf '%s\n' "$kept"; printf '%s\n' "$job"; } | crontab - && echo "Installed: $job"
    ;;
  remove)
    kept=$(without_ours)
    printf '%s\n' "$kept" | crontab - && echo "Removed."
    ;;
  show)
    current | grep -F "$TAG" || echo "Not installed."
    ;;
  *)
    sed -n '2,10p' "$0"; exit 2
    ;;
esac
