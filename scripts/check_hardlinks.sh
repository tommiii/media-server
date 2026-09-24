#!/bin/bash
# Verifies that deleting finished downloads cannot delete anything that is in your library.
#
#   ./scripts/check_hardlinks.sh
#
# qBittorrent deletes a torrent and its files some days after it finished (config/arr.yml). That is safe
# only because Sonarr/Radarr import with HARDLINKS: the file in downloads/complete and the one in media/
# are the same data under two names, so removing the first leaves the second. This script checks:
#   1. Sonarr and Radarr have "Use Hardlinks instead of Copy" on
#   2. downloads/complete and media/ are on the same filesystem (hardlinks cannot cross filesystems)
#   3. in practice: which finished files are hardlinked into media/ (safe to delete) and which are NOT
#      (archives are counted apart: Unpackerr extracts them, so the archive itself is never linked)
#      (not imported yet, or imported as a copy: deleting them would lose the only copy if they are
#      not also in media/)
#   4. the reverse: recent files in media/ that were copied instead of hardlinked (wasted space)
# Exit code 1 if something needs attention.

set -u
cd "$(dirname "$0")/.." || exit 2
[ -f .env ] || { echo ".env not found"; exit 2; }
set -a; . ./.env; set +a
: "${DATA_DIR:?DATA_DIR is not set in .env}"

MIN_SIZE=50M          # ignore small files (subtitles, nfo...)
RECENT_DAYS=7
complete="$DATA_DIR/data/downloads/complete"
media="$DATA_DIR/data/media"
fail=0
ok()   { printf 'OK    %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; fail=1; }
note() { printf 'WARN  %s\n' "$*"; }

# link count, device id, mtime (epoch), size: GNU stat on the server, BSD stat elsewhere
if stat -c '%h' / >/dev/null 2>&1; then st() { stat -c '%h %d %Y %s' "$1"; }; else st() { stat -f '%l %d %m %z' "$1"; }; fi

echo "== 1. Sonarr / Radarr settings"
api_host=${LAN_IP:-127.0.0.1}; [ "$api_host" = "0.0.0.0" ] && api_host=127.0.0.1
for pair in "Sonarr:8989:${SONARR_API_KEY:-}" "Radarr:7878:${RADARR_API_KEY:-}"; do
  IFS=: read -r app port key <<<"$pair"
  if [ -z "$key" ]; then note "$app: no API key in .env, skipped (run ./scripts/apply_arr_config.py)"; continue; fi
  cfg=$(curl -s -m 10 -H "X-Api-Key: $key" "http://$api_host:$port/api/v3/config/mediamanagement" 2>/dev/null)
  if grep -Eq '"copyUsingHardlinks" *: *true' <<<"$cfg"; then ok "$app imports with hardlinks"
  else bad "$app does not import with hardlinks (answer: ${cfg:0:60}...): run ./scripts/apply_arr_config.py"; fi
done

echo "== 2. Same filesystem"
if [ ! -d "$complete" ] || [ ! -d "$media" ]; then
  bad "missing folder: $complete or $media"
else
  read -r _ dev_c _ _ <<<"$(st "$complete")"; read -r _ dev_m _ _ <<<"$(st "$media")"
  if [ "$dev_c" = "$dev_m" ]; then ok "downloads/complete and media are on the same filesystem"
  else bad "downloads/complete (device $dev_c) and media (device $dev_m) are on different filesystems: imports are copies, not hardlinks"; fi
fi

echo "== 3. Finished downloads: hardlinked into the library?"
linked=0; alone=0; alone_list=()
while IFS= read -r -d '' f; do
  read -r nlink _ mtime size <<<"$(st "$f")"
  if [ "$nlink" -ge 2 ]; then linked=$((linked+1)); else alone=$((alone+1)); alone_list+=("$f|$mtime|$size"); fi
done < <(find "$complete" -type f -size +"$MIN_SIZE" ! \( -iname '*.rar' -o -iname '*.r[0-9][0-9]' -o -iname '*.zip' -o -iname '*.7z' \) -print0 2>/dev/null)
ok "$linked finished file(s) are hardlinked into media/: deleting the download keeps the library copy"
archives=$(find "$complete" -type f -size +"$MIN_SIZE" \( -iname '*.rar' -o -iname '*.r[0-9][0-9]' -o -iname '*.zip' -o -iname '*.7z' \) 2>/dev/null | wc -l | tr -d ' ')
[ "$archives" -gt 0 ] && note "$archives archive file(s) in downloads/complete: Unpackerr extracts them and Sonarr/Radarr import the video from the extracted copy. The archive itself is never hardlinked, by design; if the extracted video was never imported, it is not in your library"
if [ "$alone" -gt 0 ]; then
  bad "$alone finished file(s) are NOT hardlinked: not imported yet, or imported as a copy. If the cleanup deletes them and nothing else holds the data, it is gone:"
  now=$(date +%s)
  for entry in "${alone_list[@]:0:20}"; do
    IFS='|' read -r f mtime size <<<"$entry"
    printf '        %s  (%s MB, %s day(s) old)\n' "${f#"$complete"/}" "$((size/1048576))" "$(( (now-mtime)/86400 ))"
  done
  [ "$alone" -gt 20 ] && echo "        ... and $((alone-20)) more"
fi

echo "== 4. Recent library files: hardlink or copy?"
copies=0
while IFS= read -r -d '' f; do
  read -r nlink _ _ _ <<<"$(st "$f")"
  [ "$nlink" -lt 2 ] && copies=$((copies+1))
done < <(find "$media" -type f -size +"$MIN_SIZE" -mtime -"$RECENT_DAYS" -print0 2>/dev/null)
if [ "$copies" -gt 0 ]; then note "$copies file(s) added to media/ in the last $RECENT_DAYS days have no second link: they were copied (or the download is already gone). Fine for safety, but it costs disk space while the download is still seeding"; else ok "no recent copies in media/"; fi

echo
if [ "$fail" = 0 ]; then echo "Cleanup is safe: everything finished is hardlinked into the library."; else echo "Fix the FAIL lines before relying on the automatic cleanup."; fi
exit "$fail"
