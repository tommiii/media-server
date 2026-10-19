#!/bin/bash
# Installs the jobs of this repository in YOUR crontab (no root needed). Idempotent.
#
#   ./scripts/cron.sh install                  daily at 04:30: remove duplicate downloads
#                                              every minute: remove episodes grabbed before their release day (fakes)
#   ./scripts/cron.sh install --keep-manual    same, but never touch torrents you added by hand
#                                              (or set DEDUPE_ARGS=--keep-manual in .env, so setup.sh keeps it)
#   ./scripts/cron.sh remove                   take the jobs out again
#   ./scripts/cron.sh show                     print what is installed
#
# Logs: $BASE_DIR/services/dedupe.log and $BASE_DIR/services/unaired.log (see .env).
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD
[ -f .env ] && { set -a; . ./.env; set +a; }
LOG="${BASE_DIR:-$ROOT}/services/dedupe.log"
GUARD_LOG="${BASE_DIR:-$ROOT}/services/unaired.log"
TAG="# media-server: dedupe"
GUARD_TAG="# media-server: unaired"

current() { crontab -l 2>/dev/null || true; }
without_ours() { current | grep -v -F -e "$TAG" -e "$GUARD_TAG"; }

case "${1:-show}" in
  install)
    shift
    mkdir -p "$(dirname "$LOG")"
    extra="${*:-${DEDUPE_ARGS:-}}"   # e.g. --keep-manual, from the command line or DEDUPE_ARGS in .env
    job="30 4 * * * cd '$ROOT' && ./scripts/dedupe_downloads.py --apply $extra >> '$LOG' 2>&1 $TAG"
    guard="* * * * * cd '$ROOT' && ./scripts/guard_unaired.py --apply >> '$GUARD_LOG' 2>&1 $GUARD_TAG"
    kept=$(without_ours)            # read first, write after: never read and rewrite at the same time
    { [ -n "$kept" ] && printf '%s\n' "$kept"; printf '%s\n' "$job" "$guard"; } | crontab - &&
      printf 'Installed: %s\nInstalled: %s\n' "$job" "$guard"
    ;;
  remove)
    kept=$(without_ours)
    printf '%s\n' "$kept" | crontab - && echo "Removed."
    ;;
  show)
    current | grep -F -e "$TAG" -e "$GUARD_TAG" || echo "Not installed."
    ;;
  *)
    sed -n '2,10p' "$0"; exit 2
    ;;
esac
