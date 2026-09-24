# Docker Compose Media Server

Self-hosted media server where **everything that searches or downloads runs behind a Mullvad VPN** (WireGuard via [Gluetun](https://github.com/qdm12/gluetun)) with a firewall kill switch.

| Service | Role | VPN | URL |
|---|---|---|---|
| Gluetun | VPN client + kill switch | – | – |
| qBittorrent | Torrent client | ✅ | `http://<LAN_IP>:8080` |
| Prowlarr | Indexer manager | ✅ | `http://<LAN_IP>:9696` |
| FlareSolverr | Cloudflare solver for Prowlarr | ✅ | not published |
| Sonarr | TV automation | ✅ | `http://<LAN_IP>:8989` |
| Radarr | Movie automation | ✅ | `http://<LAN_IP>:7878` |
| Recyclarr | Keeps quality profiles / custom formats in sync (TRaSH Guides) | ✅ | – |
| Plex | Media streaming | ❌ | `http://<LAN_IP>:32400/web` |
| Homepage | Dashboard with widgets (login required) | ❌ | `http://<LAN_IP>` |

Prowlarr, FlareSolverr, Sonarr, Radarr and qBittorrent share Gluetun's network namespace: they have no network interface other than the VPN tunnel, so if the tunnel drops they simply have no connection (no leak). That is also why they talk to each other on `localhost`.

---

## 1. Requirements

- A **Linux** host with Docker Engine and **Docker Compose v2.20+** (`docker compose version`).
- `/dev/net/tun` on the host (`ls /dev/net/tun`; if missing: `sudo modprobe tun`).
- A **fixed LAN IP** for the server (set a DHCP reservation on your router). The UIs are bound to that address; if it changes, the containers will not start.
- A [Mullvad](https://mullvad.net) account.
- Optional: an Intel GPU (`/dev/dri`) for Plex hardware transcoding (needs Plex Pass). No GPU? Remove the `devices:` block of `plex` in `compose.yml`.

## 2. Get the code and create `.env`

```bash
git clone <this repo> media-server && cd media-server
cp .env.sample .env
chmod 600 .env
```

Edit `.env`:

| Variable | Meaning |
|---|---|
| `PUID` / `PGID` | UID/GID that owns the files (`id -u` / `id -g`) |
| `TIMEZONE` | e.g. `Europe/Amsterdam` |
| `BASE_DIR` | where configs live (`$BASE_DIR/services/<name>`), ideally on an SSD |
| `DATA_DIR` | where downloads and media live (can equal `BASE_DIR`) |
| `LAN_IP` | the server's LAN address, e.g. `192.168.1.10` |
| `LAN_SUBNETS` | subnets allowed to reach the UIs, e.g. `192.168.1.0/24` (find yours with `ip -4 route`). Add `100.64.0.0/10` if you use Tailscale |
| `HOMEPAGE_AUTH_PASSWORD` | password to open the dashboard |
| `HOMEPAGE_AUTH_SECRET` | random string that signs the session cookie: `openssl rand -base64 32` |
| `PLEX_CLAIM` | token from https://www.plex.tv/claim/ (valid 4 minutes, fill it right before the first start) |
| `WIREGUARD_ADDRESSES` | from Mullvad, see step 4 |
| `VPN_COUNTRIES` | Mullvad exit country, English name (`Netherlands`, `Sweden`, `Switzerland`, `Germany`, ...) |

> Tailscale/remote access: `LAN_IP` limits where ports are published. If you reach the server through its Tailscale IP, set `LAN_IP` to the Tailscale IP of the server (or `0.0.0.0` if the host firewall already restricts access) **and** add `100.64.0.0/10` to `LAN_SUBNETS`.

## 3. Create folders (do this before the first start)

If Docker creates the folders itself they end up owned by root and the apps cannot write to them.

```bash
set -a; . ./.env; set +a
mkdir -p "$BASE_DIR"/services/{gluetun,prowlarr,sonarr,radarr,qbittorrent,recyclarr,homepage,plex}
mkdir -p "$DATA_DIR"/data/{downloads,media/{tv,movies}}
sudo chown -R "$PUID:$PGID" "$BASE_DIR/services" "$DATA_DIR/data"
```

Resulting layout (follows [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/) so Sonarr/Radarr can hardlink and move instantly):

```
$DATA_DIR/data
├── downloads      <- qBittorrent writes here (it cannot see anything else)
└── media
    ├── tv         <- Sonarr root folder
    └── movies     <- Radarr root folder
```

## 4. Set up Mullvad (WireGuard)

1. Log in at https://mullvad.net/account with your account number.
2. Go to **WireGuard configuration** (https://mullvad.net/account/wireguard-config), choose platform **Linux**, click **Generate key**, then **Download file** (any server is fine, Gluetun picks its own).
   - Mullvad allows **5 devices per account**; every generated key counts as one. Delete unused ones on the same page.
   - Only one connection per key: if another machine/container is using this key, stop it first.
3. Open the downloaded `.conf`. You only need two lines from `[Interface]`:
   ```
   PrivateKey = <long base64 string>
   Address = 10.64.x.x/32,fc00:bbbb:...
   ```
4. Put them in place:
   ```bash
   # private key -> file (a Docker secret, not an env var, so it does not show up in `docker inspect`)
   printf '%s' 'PASTE_PRIVATE_KEY_HERE' > "$BASE_DIR/services/gluetun/wireguard_private_key"
   chmod 600 "$BASE_DIR/services/gluetun/wireguard_private_key"
   ```
   and in `.env`, **only the IPv4 part** of `Address` (before the comma):
   ```
   WIREGUARD_ADDRESSES=10.64.x.x/32
   ```
5. Delete the downloaded `.conf`: the key is now only in that secret file.

Notes: Mullvad no longer supports port forwarding, so nobody can connect *in* to your torrent client. Downloads and seeding to connectable peers still work; it is a Mullvad limitation, not a misconfiguration.

## 5. Start everything

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Wait ~30 seconds. `gluetun` must be `healthy`; the other VPN services start only after that. Then verify:

```bash
./check_vpn_connection.sh
```

You should see `OK ... You are connected to Mullvad` for gluetun, prowlarr, flaresolverr, sonarr, radarr and qbittorrent, and `OK plex is outside the VPN`. The script exits non-zero if anything is wrong. Run it again after every change to the VPN part of the compose file.

If `gluetun` is not healthy, see [Troubleshooting](#troubleshooting).

## 6. Configure the apps

Order matters: the later apps need keys/passwords from the earlier ones. In the UIs, use **`localhost`** for services behind the VPN (they share a network namespace) and the ports shown below.

### 6.1 qBittorrent — `http://<LAN_IP>:8080`

1. Get the temporary password: `docker logs qbittorrent 2>&1 | grep -i "temporary password"`. Log in as `admin`.
2. **Tools → Options → Web UI**: set your own username and password.
3. **Downloads**: *Default Save Path* = `/data/downloads`.
4. **Connection**: untick *Use UPnP / NAT-PMP port forwarding* (useless behind Mullvad).
5. **Advanced → Network Interface**: select **`tun0`** (the VPN interface inside Gluetun, also used for WireGuard). This is a second layer on top of the kill switch: qBittorrent will refuse to use any other interface.
6. Save.

### 6.2 Prowlarr — `http://<LAN_IP>:9696`

1. On first login set authentication to *Forms* and choose a username/password (**Settings → General → Authentication**, *Required*).
2. **Settings → Indexers → +** *FlareSolverr*: Host `http://localhost:8191`, Tags `flaresolverr`. Give that tag to the indexers that need Cloudflare solving.
3. **Indexers → Add Indexer**: add your indexers.
4. **Settings → Apps**: add Sonarr and Radarr later, after step 6.3 (you need their API keys).

### 6.3 Sonarr — `http://<LAN_IP>:8989` and Radarr — `http://<LAN_IP>:7878`

For each one:

1. Set authentication (*Forms*, *Required*) on first login.
2. **Settings → Media Management → Root Folders**: Sonarr `/data/media/tv`, Radarr `/data/media/movies`. Keep *Use Hardlinks instead of Copy* enabled.
3. **Settings → Download Clients → + → qBittorrent**:
   - Host `localhost`, Port `8080`
   - Username/password from step 6.1
   - Category `tv` (Sonarr) or `movies` (Radarr)
4. **Do not add indexers here.** They come from Prowlarr. Adding one directly would make Sonarr/Radarr contact it themselves (still through the VPN in this setup, but you lose the single point of control).
5. Copy the API key from **Settings → General**.

Back in Prowlarr → **Settings → Apps → +**:

| App | Prowlarr Server | App Server |
|---|---|---|
| Sonarr | `http://localhost:9696` | `http://localhost:8989` |
| Radarr | `http://localhost:9696` | `http://localhost:7878` |

paste each API key, *Test*, *Save*. Prowlarr pushes the indexers to both apps.

### 6.4 Plex — `http://<LAN_IP>:32400/web`

1. Because of `PLEX_CLAIM` the server is attached to your Plex account automatically. If you skipped it (or it expired), run `docker compose up -d --force-recreate plex` with a fresh token.
2. Add libraries. Plex mounts `$DATA_DIR` read-only as `/data/media`, so the folders are:
   - TV: `/data/media/data/media/tv`
   - Movies: `/data/media/data/media/movies`
3. **Settings → Network**: *Custom server access URLs* = `http://<LAN_IP>:32400`; *LAN Networks* = your subnet (e.g. `192.168.1.0/24`). Plex runs in bridge mode, so without this, clients on your own network can be treated as remote.
4. Hardware transcoding (Plex Pass): **Settings → Transcoder → Use hardware acceleration**.
5. Remote access: forward TCP 32400 on your router to `<LAN_IP>`, or use Tailscale (see the note in step 2).

Plex needs write access only to its own config, so the media is mounted `:ro`. If you enable *Allow media deletion* in Plex, remove the `:ro` from the `plex` volume.

### 6.5 Homepage — `http://<LAN_IP>`

The dashboard is configured **as code**: `homepage/settings.yaml`, `services.yaml` and `widgets.yaml` live in this repo and are mounted read-only, so the container cannot change them; a change is a `git commit` plus `docker compose restart homepage`. Homepage also insists on a few other, empty files (bookmarks, docker, ...): it creates them itself in `$BASE_DIR/services/homepage`, outside the repo. It has no Docker socket. You log in with `HOMEPAGE_AUTH_PASSWORD`.

The links and status dots work immediately. To turn on the widgets (download queue, Plex streams, ...), put the credentials in `.env` and recreate the container:

| `.env` variable | Where to find it |
|---|---|
| `SONARR_API_KEY`, `RADARR_API_KEY`, `PROWLARR_API_KEY` | each app → **Settings → General → API Key** |
| `QBITTORRENT_API_KEY` | qBittorrent → **Tools → Options → Web UI → Authentication → API Key** (generate one; needs qBittorrent ≥ 5.2) |
| `PLEX_TOKEN` | Plex web → open any item → **⋯ → Get Info → View XML**; copy `X-Plex-Token=...` from the URL ([details](https://www.plexopedia.com/plex-media-server/general/plex-token/)) |

```bash
docker compose up -d homepage
```

Add or reorder services in `homepage/services.yaml`. Anything written as `{{HOMEPAGE_VAR_NAME}}` in those files is replaced with the `HOMEPAGE_VAR_NAME` environment variable of the container (see the `homepage` service in `compose.yml`), so no secret ever ends up in git. Use `gluetun:<port>` for services behind the VPN and `plex:32400` for Plex when you configure widgets (Homepage reaches them over the Docker networks).

### 6.6 Recyclarr and download safety (do this once the keys are in `.env`)

Both need the Sonarr/Radarr (and qBittorrent) API keys from 6.5. Recyclarr does not sync when it starts: it only runs at its schedule (daily at 00:00 UTC), so **run the first sync by hand** as below.

```bash
docker compose up -d                                        # picks up the new keys (homepage, recyclarr)
docker compose exec recyclarr recyclarr sync --preview      # what Recyclarr would change
docker compose exec recyclarr recyclarr sync                # first sync, now (then it repeats daily by itself)
docker compose logs recyclarr
./apply_download_safety.py --check                          # what the file filters would change
./apply_download_safety.py                                  # apply them
```

- **Recyclarr** reads `recyclarr/configs/media.yml` (in this repo, mounted read-only). It also sets the per-quality **size limits** ([File size](#file-size-what-is-configured)). It creates the TRaSH profiles **WEB-1080p** (Sonarr) and **HD Bluray + WEB** (Radarr) with their custom formats. It does not touch your existing profiles: assign the new one to your series/movies (Sonarr: *Series → Mass Editor*; Radarr: *Movies → Mass Editor*) and to your defaults. For 2160p, remux or anime, take another template from https://github.com/recyclarr/config-templates and put it in `recyclarr/configs/`.
- **`apply_download_safety.py`** (file filters and the Radarr size ceiling) is idempotent: run it again whenever Prowlarr adds indexers to Sonarr/Radarr (the setting it enforces is per indexer, see below).

## Only video files: how downloads are controlled

Sonarr/Radarr do **not** reject executables out of the box: the per-indexer *Fail Downloads* option is empty by default. That is why `.exe` files can reach your downloads folder. Layers, from the earliest to the last:

| # | Where | What it does |
|---|---|---|
| 1 | **Recyclarr** (TRaSH custom formats) | Scores down / blocks BR-DISK, LQ, fake and low-quality release groups: the usual carriers of junk |
| 2 | **Sonarr/Radarr, per indexer: *Fail Downloads*** (set by `apply_download_safety.py`) | Reads the file list inside the `.torrent` **before** sending it to qBittorrent. A release with `.exe .bat .cmd .sh`, "potentially dangerous" files (`.lnk .scr .ps1 .vbs .arj .lzh .zipx`) or, in Sonarr, your extra extensions (`.msi .js .jar .dll .apk ...`) is rejected, blocklisted, and the next best release is tried. The same check runs again at import. Look for *"Caution: Found executable..."* in Activity/History: that is it working |
| 3 | **qBittorrent, *Excluded file names*** (set by the script) | Whatever slips through (magnet links have no file list to inspect) is never written to disk if it matches `*.exe *.msi *.bat *.scr ...`. External-program hooks ("run on torrent added/finished") are disabled |
| 4 | **Sonarr/Radarr import** | Only files with a video extension are ever moved into the library |
| 5 | **`./audit_media.sh`** | Checks the real content, not the name: a video extension must correspond to a video file (magic bytes), so an executable renamed to `movie.mkv` is caught. Anything that is not video/subtitle/artwork is reported. `--quarantine` moves it aside (never deletes). Exit code 1 when something is found, so it can run from cron, e.g. `0 5 * * * cd /path/to/media-server && ./audit_media.sh` |
| 6 | *(optional)* **`noexec` on the data disk** | In `/etc/fstab` add `noexec,nosuid,nodev` to the mount options of the disk that holds `DATA_DIR`: nothing stored there can be executed, whatever it is |

Honest limits: extension and container checks do not detect a *valid* video file crafted to exploit a player. Keep Plex and your players updated (bump the Plex tag regularly).

## File size: what is configured

Radarr and Sonarr judge a release by **MB per minute of runtime, per quality** (a 120-minute film at 60 MB/min is 7.2 GB). A release above `max` is rejected, one below `min` too, and among acceptable ones the closest to `preferred` wins. TRaSH's default is "max 2000, preferred 1999", i.e. no limit and the biggest file wins, which is how 50 GB remuxes get picked. `recyclarr/configs/media.yml` overrides it:

| Quality | Radarr: preferred / max (120 min film) | Sonarr: preferred / max (45 min episode) |
|---|---|---|
| WEB-DL / WEBRip 1080p | 3.6 GB / 7.2 GB | 1.1 GB / 2.7 GB |
| Bluray 1080p | 8.4 GB / 13 GB | 2.7 GB / 4.5 GB |
| WEB-DL / WEBRip 2160p | 12 GB / 19 GB | 2.7 GB / 6.8 GB |
| Bluray 2160p | 18 GB / 24 GB | 5.4 GB / 9 GB |
| Remux (1080p and 2160p) | never fits (cap ≈ minimum) | never fits |

What you can turn, from the biggest effect to the smallest:

1. **Which qualities are allowed at all** (quality profile). The synced profiles (*WEB-1080p*, *HD Bluray + WEB*) are 1080p only, so no 4K and no remux. For 4K, add the `uhd-bluray-web` (Radarr) / `web-2160p` (Sonarr) template from https://github.com/recyclarr/config-templates and assign it only to the titles you want in 4K. The 2160p caps above already apply to any profile.
2. **Per-quality size limits**: edit `preferred` / `max` in `media.yml`, then `docker compose exec recyclarr recyclarr sync`. The formula is `MB/min × runtime`. `min` is left at TRaSH's value, which rejects very small encodes (Bluray 1080p under ~6 GB per 2 h film, WEB-DL under ~1.5 GB). If you like 2–3 GB encodes tagged Bluray, lower `min` for `Bluray-1080p` (e.g. 20): expect lower quality.
3. **A hard ceiling per release** in Radarr: 25 GB (`MAX_RELEASE_MB` in `apply_download_safety.py`, shown in Radarr under *Settings → Indexers → Maximum Size*). It also catches releases with a wrong or unknown quality label. Sonarr has none on purpose, because it would reject season packs.
4. **Smaller codec (x265)**. TRaSH's *Golden Rule HD* prefers x264 at 1080p because x265 is often a lower-quality re-encode. If you want smaller files and your players decode HEVC, in the Radarr *Golden Rule HD* group of `media.yml` set:
   ```yaml
   - trash_id: f8bf8eab4617f12dfdbd16303d8da245 # [Optional] Golden Rule HD
     exclude:
       - dc98083864ea246d05a42df0d05f81cc # x265 (HD)
     select:
       - 839bea857ed2c0a8e084f3cbdbd65ecb # x265 (no HDR/DV)
   ```
5. **One-off exceptions**: *Interactive Search* still lists rejected releases (with the reason, e.g. "larger than maximum allowed") and lets you force a grab.

Files you already have are not touched. Rejections show up in *Interactive Search* and in the app logs; the ceiling is checked at grab time, before anything is sent to qBittorrent.

## 7. Day to day

```bash
docker compose ps                    # status
docker compose logs -f gluetun       # VPN logs
./check_vpn_connection.sh            # is everything on the VPN?
./audit_media.sh                     # is everything in downloads/media really video?
./apply_download_safety.py           # re-apply file filters (after adding indexers)
./restart_services.sh                # pull + recreate everything
```

**Updates: Renovate opens a pull request, you merge it.** Images are pinned to exact versions (no auto-updater, no Docker socket for anybody). [Renovate](https://github.com/apps/renovate) watches `compose.yml` and proposes new versions as pull requests; nothing changes on the server until you merge one and pull it.

One-time setup:

1. Commit and push this repo to GitHub (Renovate reads `renovate.json` from the default branch).
2. Install the free **Renovate** GitHub App (https://github.com/apps/renovate) and give it access to this repository only.
3. Within minutes it opens a **"Pin Docker digests"** PR: it adds `@sha256:...` next to every tag, so a tag that is moved after publication can never silently give you a different image. Merge it once, then deploy (below).

How it behaves (`renovate.json`):

- **Mondays before 08:00** (Europe/Amsterdam) it opens or refreshes its PRs; the rest of the week it stays quiet.
- **One PR per image, always the newest version.** If a newer release appears while a PR is still open, that same PR is updated to the newer version (branch and title), so you never have an old and a new PR for the same image. If you close a PR without merging, it will not come back for that version, only for a newer one. There is also a *Dependency Dashboard* issue on GitHub listing everything pending.
- **3-day cool-down**: a release is proposed only when it is at least 3 days old, which is when a broken or compromised release is usually pulled or fixed. Rebuilds of the linuxserver images (new base-image security patches, same app version) count as updates too.
- **Never auto-merges.** Read the PR (release notes are linked), merge it from the GitHub app on your phone.

Deploy after merging:

```bash
git pull && ./restart_services.sh && ./check_vpn_connection.sh
```

Something broke? `git revert <merge commit>`, then the same command.

**Backup**: `$BASE_DIR/services` (all app configs) and the Mullvad key file. Media does not need the containers.

## Security model

- Only Plex and Homepage are outside the VPN. Everything that searches or downloads shares Gluetun's network namespace and its firewall (kill switch, DNS over TLS through the tunnel).
- qBittorrent is also bound to `tun0`.
- Nothing is published on `0.0.0.0` unless you set `LAN_IP=0.0.0.0`. This matters because Docker-published ports bypass ufw/firewalld.
- FlareSolverr (no authentication) is not published at all.
- No container has the Docker socket.
- Plex and the VPN group are on separate Docker networks. Homepage is the only container on both (it needs to query both), with a read-only config and a login.
- All containers use `no-new-privileges`; every container except Gluetun also uses `cap_drop: ALL` (only the capabilities needed by the linuxserver init are added back). If a container refuses to start, remove `cap_drop` for that service and check `docker compose logs <service>`.
- The Mullvad key is a Docker secret and `.env` is git-ignored.
- Set a password on every web UI (steps 6.1–6.3). The apps run inside the VPN but they are still reachable by anyone on your LAN.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `required variable LAN_IP is missing` | Fill `LAN_IP` and `LAN_SUBNETS` in `.env` |
| `gluetun` stays `unhealthy` / restarts | `docker compose logs gluetun`. Check: private key file has no trailing newline or spaces; `WIREGUARD_ADDRESSES` is the IPv4 `/32` only; the key is registered in your Mullvad account (generated on the site) and you are within 5 devices; `VPN_COUNTRIES` is a valid English country name; no other client uses the same key |
| `/dev/net/tun: no such file` | `sudo modprobe tun` (persist with `echo tun \| sudo tee /etc/modules-load.d/tun.conf`) |
| `cannot assign requested address` / port bind errors | `LAN_IP` is not an address of this host, or the port (80, 8080, 32400, ...) is already used by something else |
| UIs not reachable from your PC | Your PC's subnet must be in `LAN_SUBNETS`, and you must use `http://<LAN_IP>:<port>` |
| Sonarr/Radarr cannot reach qBittorrent or Prowlarr | Use `localhost`, not container names (they share a network namespace) |
| `Permission denied` in the apps | Re-run the `chown` in step 3; `PUID`/`PGID` must match the folder owner |
| A linuxserver container exits right after start | Remove `cap_drop: ALL` for that service and read its logs |
| Prowlarr indexers fail with Cloudflare errors | The indexer needs the `flaresolverr` tag (6.2). Some indexers also block Mullvad IPs: try another `VPN_COUNTRIES` |
| Sonarr/Radarr metadata (TVDB/TMDB) errors | Rarely, a service blocks VPN IPs. Try another `VPN_COUNTRIES`; as a last resort Sonarr/Radarr can be moved out of the VPN (this lowers protection, so keep indexers only in Prowlarr) |
| Homepage: `Host validation failed` | The address you type in the browser must be in `HOMEPAGE_ALLOWED_HOSTS` (default: `LAN_IP`). Add other names with `HOMEPAGE_ALLOWED_HOSTS=192.168.1.10,server.tailnet.ts.net` in `.env` |
| Homepage widgets show errors or no numbers | The matching key in `.env` is empty or wrong (6.5); run `docker compose up -d homepage` after editing `.env` |
| `recyclarr sync` reports `api_key`/401/connection errors | `SONARR_API_KEY` / `RADARR_API_KEY` are empty or wrong in `.env`; fix and `docker compose up -d recyclarr` |
| `apply_download_safety.py`: `skipped ... is empty` or connection errors | Fill the API keys in `.env`; the script talks to `http://<LAN_IP>:<port>`, so run it from a machine in `LAN_SUBNETS` (the server itself is fine) |
| A release disappeared / "Caution: Found executable" in Activity | The protection worked: the release was blocklisted and another one is tried |
| Plex clients on the LAN play "remotely" | Set *LAN Networks* and *Custom server access URLs* (6.4) |
| Plex says the server is not claimed | Generate a new claim token, put it in `.env`, `docker compose up -d --force-recreate plex` |

## Migrating from the previous setup (WireGuard + Deluge + Overseerr + Watchtower)

1. Let active downloads finish, or note what is in Deluge: qBittorrent starts empty. Finished files stay in `$DATA_DIR/data/downloads`.
2. Update the repo and follow steps 2–4 above (`.env`, folders, Mullvad key).
3. Run `./restart_services.sh`: it stops the old stack and removes its leftover containers (`down --remove-orphans`) *before* starting Gluetun, which matters because only one client may use the Mullvad key at a time. Then continue from step 5 (`./check_vpn_connection.sh`).
4. Sonarr/Radarr: replace the Deluge client with qBittorrent (6.3) and change Prowlarr/Sonarr/Radarr hosts from `wireguard`/IPs to `localhost` (6.2, 6.3).
5. You can delete `$BASE_DIR/services/{deluge,overseerr,wireguard,homarr}` when you are sure you do not need them. Requests made in Overseerr are not migrated.
