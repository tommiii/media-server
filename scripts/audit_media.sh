#!/bin/bash
# Checks that everything in downloads/ and media/ is really video (or a subtitle/artwork/nfo next to it).
# downloads/incomplete is skipped: unfinished files are not video yet.
#
#   ./scripts/audit_media.sh               report only
#   ./scripts/audit_media.sh --quarantine  also move suspicious files to $DATA_DIR/data/quarantine/ (nothing is deleted)
#
# Two checks per file:
#   1. extension: only video, subtitle and artwork/nfo extensions are expected
#   2. content:   files with a video extension must be identified as video by `file` (magic bytes),
#                 so an executable renamed to movie.mkv is caught
# Exit code is 1 if anything is flagged, so it can run from cron.

set -u
cd "$(dirname "$0")/.." || exit 2   # repository root
[ -f .env ] || { echo ".env not found"; exit 2; }
set -a; . ./.env; set +a
command -v file >/dev/null || { echo "'file' is required (apt install file)"; exit 2; }

root="$DATA_DIR/data"
quarantine="$root/quarantine"
move=0; [ "${1:-}" = "--quarantine" ] && move=1

video_ext='mkv|mp4|m4v|avi|mov|ts|m2ts|wmv|webm|mpg|mpeg'
other_ext='srt|ass|ssa|sub|idx|sup|vtt|nfo|txt|jpg|jpeg|png|webp|tbn'
flagged=0

flag() { # file reason
  printf 'FLAG  %s\n      %s\n' "$1" "$2"
  flagged=$((flagged + 1))
  if [ "$move" = 1 ]; then
    dest="$quarantine/${1#"$root"/}"
    mkdir -p "$(dirname "$dest")" && mv -n -- "$1" "$dest" && echo "      -> moved to $dest"
  fi
}

while IFS= read -r -d '' f; do
  ext="${f##*.}"; ext="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
  mime="$(file -b --mime-type -- "$f" | tail -n1)"; mime="${mime##*[[:space:]]}"   # macOS prints one line per arch
  case "$mime" in
    application/x-dosexec|application/x-executable|application/x-sharedlib|application/x-pie-executable|application/x-mach-binary|application/x-msdownload)
      flag "$f" "executable content ($mime)"; continue ;;
  esac
  if [[ "$ext" =~ ^($video_ext)$ ]]; then
    [[ "$mime" == video/* || "$mime" == application/x-matroska || "$mime" == application/vnd.rn-realmedia ]] \
      || flag "$f" "video extension but content is '$mime'"
  elif [[ ! "$ext" =~ ^($other_ext)$ ]]; then
    case "$ext" in '!qb'|parts|part) continue ;; esac   # partial downloads
    flag "$f" "unexpected file type '.$ext' ($mime)"
  fi
done < <(find "$root/downloads" "$root/media" -path "$root/downloads/incomplete" -prune -o -type f -print0 2>/dev/null)

echo
if [ "$flagged" -eq 0 ]; then echo "OK: nothing suspicious found."; else echo "$flagged file(s) flagged."; fi
[ "$flagged" -eq 0 ]
