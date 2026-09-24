#!/bin/bash
# Checks that nothing that searches or downloads can reach the internet outside the VPN.
#
#   ./scripts/leak_test.sh                 read-only checks
#   ./scripts/leak_test.sh --kill-switch   also takes the tunnel down for a moment (downloads pause ~30 s)
#
# 1. Structure: every running container of the stack, except OUTSIDE_VPN below, must live in gluetun's
#    network namespace. A service added to compose.yml without `network_mode: service:gluetun`
#    (a second torrent client, say) is caught here, before it ever leaks.
# 2. Exit IP seen from inside: it must be a Mullvad exit and different from your home IP.
# 3. DNS: containers must resolve through gluetun (127.0.0.1), not through your router or Docker.
# 4. qBittorrent must be bound to tun0.
# 5. (--kill-switch) with the tunnel down, the torrent client must have no connectivity at all.
#
# Exit code 1 if anything failed: run it after every change to compose.yml.

set -u
cd "$(dirname "$0")/.." || exit 2

# Containers allowed to have their own network. Everything else must be inside gluetun's.
OUTSIDE_VPN=(gluetun plex homepage)
PROBE=qbittorrent                      # container used for the exit-IP and kill-switch probes
MULLVAD="https://am.i.mullvad.net"

fail=0
ok()   { printf 'OK    %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; fail=1; }
note() { printf 'WARN  %s\n' "$*"; }

fetch() {  # container url -> body (curl, or wget in images without it)
  docker exec "$1" sh -c "curl -s -m 10 '$2' 2>/dev/null || wget -qO- -T 10 '$2' 2>/dev/null"
}

[ -f .env ] && { set -a; . ./.env; set +a; }

gluetun_id=$(docker inspect -f '{{.Id}}' gluetun 2>/dev/null) || { echo "gluetun is not running"; exit 1; }
home_ip=$(curl -s -m 10 "$MULLVAD/ip" 2>/dev/null | tr -d '[:space:]')

echo "== 1. Network namespaces"
for id in $(docker compose ps -q); do
  name=$(docker inspect -f '{{.Name}}' "$id" | tr -d /)
  mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$id")
  if [[ " ${OUTSIDE_VPN[*]} " == *" $name "* ]]; then
    ok "$name is outside the VPN by design"
  elif [ "$mode" = "container:$gluetun_id" ]; then
    ok "$name has no network of its own: only gluetun's tunnel"
  else
    bad "$name is NOT inside gluetun's network (mode: $mode): its traffic does not go through the VPN"
  fi
done

echo "== 2. Exit IP"
body=$(fetch "$PROBE" "$MULLVAD/json")
exit_ip=$(grep -Eo '"ip" *: *"[^"]+"' <<<"$body" | head -1 | cut -d'"' -f4)
if ! grep -Eq '"mullvad_exit_ip" *: *true' <<<"$body"; then
  bad "$PROBE does not exit through a Mullvad server (answer: ${body:-none})"
elif [ -n "$home_ip" ] && [ "$exit_ip" = "$home_ip" ]; then
  bad "$PROBE exits with your home IP $home_ip"
else
  ok "$PROBE exits through Mullvad as $exit_ip (your home IP: ${home_ip:-unknown})"
fi

echo "== 3. DNS"
resolvers=$(docker exec "$PROBE" cat /etc/resolv.conf 2>/dev/null | awk '/^nameserver/{print $2}')
if [ -z "$resolvers" ]; then
  note "cannot read the resolvers of $PROBE"
elif [ "$(echo "$resolvers" | sort -u)" = "127.0.0.1" ]; then
  ok "$PROBE resolves names through gluetun (127.0.0.1, DNS over TLS inside the tunnel)"
else
  bad "$PROBE uses resolvers other than gluetun's: $(echo $resolvers). Names would be asked outside the tunnel"
fi

echo "== 4. qBittorrent interface"
if [ -n "${QBITTORRENT_API_KEY:-}" ] && [ -n "${LAN_IP:-}" ]; then
  api_host=$LAN_IP; [ "$api_host" = "0.0.0.0" ] && api_host=127.0.0.1
  iface=$(curl -s -m 10 -H "Authorization: Bearer $QBITTORRENT_API_KEY" "http://$api_host:8080/api/v2/app/preferences" 2>/dev/null \
          | grep -Eo '"current_network_interface" *: *"[^"]*"' | cut -d'"' -f4)
  if [ "$iface" = "tun0" ]; then ok "qBittorrent is bound to tun0"; else bad "qBittorrent interface is '${iface:-unknown}', expected tun0 (run ./scripts/apply_arr_config.py)"; fi
else
  note "skipped: QBITTORRENT_API_KEY or LAN_IP is not set in .env"
fi

if [ "${1:-}" = "--kill-switch" ]; then
  echo "== 5. Kill switch (the tunnel goes down now)"
  docker exec gluetun ip link set tun0 down
  sleep 2
  leak=$(fetch "$PROBE" "$MULLVAD/ip" | tr -d '[:space:]')
  if [ -z "$leak" ]; then
    ok "with the tunnel down $PROBE has no connectivity at all"
  elif [ -n "$home_ip" ] && [ "$leak" = "$home_ip" ]; then
    bad "LEAK: with the tunnel down $PROBE reached the internet as $leak (your home IP)"
  else
    note "$PROBE still answered ($leak): the tunnel probably came back before the probe, run again"
  fi
  echo "   waiting for gluetun to bring the tunnel back..."
  back=0
  for _ in $(seq 1 40); do
    sleep 3
    if fetch "$PROBE" "$MULLVAD/ip" | tr -d '[:space:]' | grep -q '^[0-9a-f.:]\+$'; then back=1; break; fi
  done
  if [ "$back" = 0 ]; then
    note "tunnel did not recover by itself: restarting gluetun"
    docker compose restart gluetun >/dev/null && ok "gluetun restarted" || bad "could not restart gluetun"
  else
    ok "tunnel is back"
  fi
fi

echo
if [ "$fail" = 0 ]; then echo "No leak found."; else echo "LEAK RISK: fix the FAIL lines above."; fi
exit "$fail"
