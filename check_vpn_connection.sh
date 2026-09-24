#!/bin/bash
# Verifies that gluetun and everything sharing its network namespace exits through Mullvad.
# Exit code is non-zero if any check fails.

fail=0
check() {
  local name=$1 out
  out=$(docker exec "$name" sh -c 'curl -s -m 10 https://am.i.mullvad.net/connected || wget -qO- -T 10 https://am.i.mullvad.net/connected' 2>&1)
  if grep -q "You are connected to Mullvad" <<<"$out"; then
    echo "OK   $name: $out"
  else
    echo "FAIL $name: ${out:-no response}"
    fail=1
  fi
}

# gluetun uses wget (no curl in the image); the others share its namespace
for c in gluetun prowlarr flaresolverr sonarr radarr qbittorrent; do
  check "$c"
done

# these must NOT be on the VPN
for c in plex; do
  out=$(docker exec "$c" curl -s -m 10 https://am.i.mullvad.net/connected 2>&1)
  if grep -q "You are connected to Mullvad" <<<"$out"; then
    echo "WARN $c is going through the VPN"
    fail=1
  else
    echo "OK   $c is outside the VPN"
  fi
done

exit $fail
