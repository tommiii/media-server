#!/bin/bash

echo "Wireguard:"
docker exec wireguard curl -s https://am.i.mullvad.net/connected

echo "Deluge:"
docker exec deluge curl -s https://am.i.mullvad.net/connected

echo "Prowlarr:"
docker exec prowlarr curl -s https://am.i.mullvad.net/connected

