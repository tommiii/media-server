# Docker Compose Media Server

This repository contains a `docker-compose` setup to manage a self-hosted media server, including tools for managing and streaming media, downloading and organizing content, and ensuring network security.

## Features
- **Homarr**: A visually appealing dashboard to manage and launch your services.
- **Plex**: A media server to stream your personal library of movies, TV shows, music, and photos.
- **Sonarr & Radarr**: Automation tools for TV shows and movies.
- **WireGuard**: A VPN solution for secure connections.
- **Prowlarr**: Indexer manager for Sonarr and Radarr.
- **FlareSolverr**: CAPTCHA-solving proxy for enhanced access to restricted sites.
- **Deluge**: Torrent client with support for VPN routing.
- **Overseerr**: Media request and management platform.
- **Watchtower**: Automatic container updates.

## Prerequisites
1. **Docker** and **Docker Compose** installed on your system.
2. **Environment Variables**:
   - `PUID`: User ID for file permissions.
   - `PGID`: Group ID for file permissions.
   - `TIMEZONE`: System timezone (e.g., `America/New_York`).
   - `BASE_DIR`: Base directory for service configurations.
   - `DATA_DIR`: Directory for media files.
   - `DOCKER_SOCK`: Path to Docker socket.

   Create a `.env` file in the root directory:
   ```env
   PUID=1000
   PGID=1000
   TIMEZONE=America/New_York
   BASE_DIR=/path/to/base
   DATA_DIR=/path/to/media
   DOCKER_SOCK=/var/run/docker.sock

3. **Set Wireguard with Mullvad **:
   Download the conf file from Mullvad, edit it with this:

   ```
   [Interface]
   PrivateKey = <private key>
   Address = 9.8.7.6/32
   DNS = 8.8.8.8
   PostUp = DROUTE=$(ip route | grep default | awk '{print $3}'); HOMENET=192.168.0.0/16; HOMENET2=10.0.0.0/8; HOMENET3=172.16.0.0/12; ip route add $HOMENET3 via $DROUTE;ip route add $HOMENET2 via $DROUTE; ip route add $HOMENET via $DROUTE;iptables -I OUTPUT -d $HOMENET -j ACCEPT;iptables -A OUTPUT -d $HOMENET2 -j ACCEPT; iptables -A OUTPUT -d $HOMENET3 -j ACCEPT;  iptables -A OUTPUT ! -o %i -m mark ! --mark $(wg show %i fwmark) -m addrtype ! --dst-type LOCAL -j REJECT
   PreDown = DROUTE=$(ip route | grep default | awk '{print $3}'); HOMENET=192.168.0.0/16; HOMENET2=10.0.0.0/8; HOMENET3=172.16.0.0/12; ip route del $HOMENET3 via $DROUTE;ip route del $HOMENET2 via $DROUTE; ip route del $HOMENET via $DROUTE; iptables -D OUTPUT ! -o %i -m mark ! --mark $(wg show %i fwmark) -m addrtype ! --dst-type LOCAL -j REJECT; iptables -D OUTPUT -d $HOMENET -j ACCEPT; iptables -D OUTPUT -d $HOMENET2 -j ACCEPT; iptables -D OUTPUT -d $HOMENET3 -j ACCEPT
   ```

   if you're using Tailscale PostUp and PreDown should be:

   ```
   PostUp = DROUTE=$(ip route | grep default | awk '{print $3}'); HOMENET=192.168.0.0/16; HOMENET2=10.0.0.0/8; HOMENET3=172.16.0.0/12; HOMENET4=100.64.0.0/10; ip route add $HOMENET4 via $DROUTE; ip route add $HOMENET3 via $DROUTE; ip route add $HOMENET2 via $DROUTE; ip route add $HOMENET via $DROUTE; iptables -I OUTPUT -d $HOMENET -j ACCEPT; iptables -A OUTPUT -d $HOMENET2 -j ACCEPT; iptables -A OUTPUT -d $HOMENET3 -j ACCEPT; iptables -A OUTPUT -d $HOMENET4 -j ACCEPT; iptables -A OUTPUT ! -o %i -m mark ! --mark $(wg show %i fwmark) -m addrtype ! --dst-type LOCAL -j REJECT
   PreDown = DROUTE=$(ip route | grep default | awk '{print $3}'); HOMENET=192.168.0.0/16; HOMENET2=10.0.0.0/8; HOMENET3=172.16.0.0/12; HOMENET4=100.64.0.0/10; ip route del $HOMENET4 via $DROUTE; ip route del $HOMENET3 via $DROUTE; ip route del $HOMENET2 via $DROUTE; ip route del $HOMENET via $DROUTE; iptables -D OUTPUT ! -o %i -m mark ! --mark $(wg show %i fwmark) -m addrtype ! --dst-type LOCAL -j REJECT;iptables -D OUTPUT -d $HOMENET -j ACCEPT;iptables -D OUTPUT -d $HOMENET2 -j ACCEPT;iptables -D OUTPUT -d $HOMENET3 -j ACCEPT;iptables -D OUTPUT -d $HOMENET4 -j ACCEPT
   ```

   Save the file and place it {BASE_DIR}/services/wireguard/wg_confs



# Services Overview

## Homarr
- **Purpose**: Centralized dashboard to access and manage your services.  
- **Access**: [http://localhost](http://localhost)

## Plex
- **Purpose**: Media server for streaming personal content.  
- **Access**: [http://localhost:32400/web](http://localhost:32400/web)

## Sonarr
- **Purpose**: Manage and automate TV show downloads.  
- **Access**: [http://localhost:8989](http://localhost:8989)

## Radarr
- **Purpose**: Manage and automate movie downloads.  
- **Access**: [http://localhost:7878](http://localhost:7878)

## WireGuard
- **Purpose**: VPN solution for secure network traffic.  

## Prowlarr
- **Purpose**: Manage and integrate indexers with Sonarr and Radarr.  

## FlareSolverr
- **Purpose**: Solve CAPTCHAs for automated downloads.  

## Deluge
- **Purpose**: Torrent client with VPN support.  
- **Access**: [http://localhost:8112](http://localhost:8112)

## Overseerr
- **Purpose**: Media request platform for Plex.  
- **Access**: [http://localhost:5055](http://localhost:5055)

## Watchtower
- **Purpose**: Automatically update Docker containers.  
