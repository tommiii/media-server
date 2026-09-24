#!/bin/bash
# From "containers not started" to "everything configured", without opening any web UI.
# Safe to run again at any time: every step only changes what differs.
#
#   ./setup.sh
#
# Before the first run: .env filled in (README step 2), folders created (step 3), Mullvad key in
# place (step 4). For Plex, PLEX_CLAIM in .env if the server has never been linked to your account.
set -uo pipefail
cd "$(dirname "$0")"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
failed=()
run() { "$@" || failed+=("$*"); }

[ -f .env ] || { echo ".env not found: cp .env.sample .env and fill it in (README step 2)"; exit 1; }
command -v docker >/dev/null || { echo "docker is not installed"; exit 1; }
python3 -c "import yaml" 2>/dev/null || { echo "PyYAML is missing: sudo apt install python3-yaml"; exit 1; }

step "Checking .env and compose.yml"
docker compose config -q || { echo "compose.yml or .env is not valid (see the message above)"; exit 1; }

step "Creating the download folders (complete / incomplete)"
set -a; . ./.env; set +a
[ -n "${DATA_DIR:-}" ] || { echo "DATA_DIR is not set in .env"; exit 1; }
mkdir -p "$DATA_DIR"/data/downloads/complete "$DATA_DIR"/data/downloads/incomplete \
  || { echo "cannot create the folders: fix ownership first (README step 3)"; exit 1; }

step "Starting the VPN and waiting for it to be healthy"
docker compose up -d gluetun || exit 1
for _ in $(seq 1 60); do
  state=$(docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null || echo missing)
  [ "$state" = healthy ] && break
  sleep 3
done
if [ "$state" != healthy ]; then
  echo "gluetun is '$state' after 3 minutes: the VPN does not connect. Last log lines:"
  docker compose logs --no-log-prefix --tail 15 gluetun
  echo "See the README troubleshooting table (Mullvad key and address must be a matching pair)."
  exit 1
fi

step "Starting everything else"
docker compose up -d || exit 1

step "Configuring qBittorrent, Sonarr, Radarr, Prowlarr (indexers included) and Plex from arr.yml"
run ./apply_arr_config.py

step "Recreating containers that read the new API keys (Homepage, Recyclarr)"
run docker compose up -d

step "Recyclarr: quality profiles, custom formats and size limits"
run docker compose exec -T recyclarr recyclarr sync

step "Assigning the profiles (only where assign_to_existing is true in arr.yml; default: no)"
run ./apply_arr_config.py

step "File filters (executables are rejected) and size ceiling"
run ./apply_download_safety.py

step "Checking that everything goes through the VPN"
run ./check_vpn_connection.sh

echo
if [ ${#failed[@]} -eq 0 ]; then
  echo "All done. Nothing needed a web UI."
else
  echo "Finished, but these steps reported problems (read the messages above them):"
  printf '  - %s\n' "${failed[@]}"
  exit 1
fi
