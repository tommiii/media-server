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
| Unpackerr | Extracts releases that come as archives so Sonarr/Radarr can import them | ✅ | – |
| Plex | Media streaming, **reachable from the internet** (router port forward) | ❌ | `http://<any server address>:32400/web` |
| Homepage | Dashboard with widgets (login required) | ❌ | `http://<LAN_IP>` |

Prowlarr, FlareSolverr, Sonarr, Radarr, qBittorrent, Recyclarr and Unpackerr share Gluetun's network namespace: they have no network interface other than the VPN tunnel, so if the tunnel drops they simply have no connection (no leak). That is also why they talk to each other on `localhost`.

---

## Quick start

1. `git clone <this repo> media-server && cd media-server`, then `cp .env.sample .env && chmod 600 .env` and fill in `.env` (step 2).
2. Create the folders (step 3) and put your Mullvad key in place (step 4).
3. Run **`./scripts/setup.sh`**: it starts everything and configures qBittorrent, Sonarr, Radarr, Prowlarr, Plex and the quality profiles from `config/arr.yml`, without you opening a web UI (section 6.0).
4. **Open TCP port 32400 on your router** and point it to the server, so Plex works from outside your home (see [Open the Plex port on your router](#open-the-plex-port-on-your-router)).
5. Open the dashboard at `http://<LAN_IP>` (password: `HOMEPAGE_AUTH_PASSWORD`).

What cannot be scripted: linking Plex to your account the first time (`PLEX_CLAIM`, point 2 of section 6.0) and the router port forward (only your router can do that). Everything below is the detail, in order.

## Repository layout

```
compose.yml            the stack (containers, networks, ports)
.env.sample            copy to .env and fill in (never committed)
renovate.json          image update pull requests
scripts/               setup.sh (one command for everything), apply_arr_config.py, apply_download_safety.py,
                       leak_test.sh, check_hardlinks.sh
config/
  arr.yml              desired state of qBittorrent, Sonarr, Radarr, Prowlarr (indexers) and Plex
  recyclarr/           quality profiles and size limits (TRaSH)
  homepage/            dashboard
services/              created on the server: each app's own data (git-ignored)
```

All scripts can be run from any directory; they find the repository root themselves.

## 1. Requirements

- A **Linux** host with Docker Engine and **Docker Compose v2.20+** (`docker compose version`).
- `/dev/net/tun` on the host (`ls /dev/net/tun`; if missing: `sudo modprobe tun`).
- A **fixed LAN IP** for the server (set a DHCP reservation on your router). The web UIs are bound to the address in `LAN_IP` (your LAN address, or the server's Tailscale address); if it changes, the containers will not start.
- A [Mullvad](https://mullvad.net) account.
- To use Plex from outside your home: a router port forward of **TCP 32400** to the server, and an ISP that gives you a reachable public IPv4 (not CGNAT).
- `python3` with PyYAML for the setup script: `sudo apt install python3-yaml`. Also `curl` (leak test) and `file` (media audit): `sudo apt install curl file`.
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
| `ARR_USERNAME` / `ARR_PASSWORD` | login you choose for the Sonarr, Radarr and Prowlarr web UIs (set by `apply_arr_config.py`) |
| `QBITTORRENT_USERNAME` / `QBITTORRENT_PASSWORD` | login you choose for the qBittorrent web UI (also set by the script). Keep the username `admin` if you do not care |
| `HOMEPAGE_AUTH_PASSWORD` | password to open the dashboard |
| `HOMEPAGE_AUTH_SECRET` | random string that signs the session cookie: `openssl rand -base64 32` |
| `PLEX_CLAIM` | token from https://www.plex.tv/claim/ (valid 4 minutes, fill it right before the first start) |
| `WIREGUARD_ADDRESSES` | from Mullvad, see step 4 |
| `VPN_COUNTRIES` | Mullvad exit country, English name (`Netherlands`, `Sweden`, `Switzerland`, `Germany`, ...) |
| `SONARR_API_KEY`, `RADARR_API_KEY`, `PROWLARR_API_KEY`, `QBITTORRENT_API_KEY`, `PLEX_TOKEN` | leave empty: `apply_arr_config.py` fills them in |
| *Optional:* `PLEX_BIND_IP`, `PLEX_CUSTOM_URLS`, `HOMEPAGE_ALLOWED_HOSTS` | see the comments in `.env.sample`: address Plex is published on (default: all IPv4), extra URLs Plex advertises, extra host names allowed to open the dashboard |
| *Optional:* `LOG_LEVEL`, `LOG_HTML`, `CAPTCHA_SOLVER` | FlareSolverr settings (defaults `info`, `false`, `none`) |

Put values that contain spaces, `$` or `#` in single quotes, e.g. `ARR_PASSWORD='my pass#1'`: the file is read by Docker Compose, by the shell (step 3) and by the scripts.

> Tailscale/remote access: `LAN_IP` limits where the **web UIs** are published (Plex is different: it is published on every IPv4 address so it can be reached from the internet, see `PLEX_BIND_IP` in `.env.sample`). If you reach the server through its Tailscale IP, set `LAN_IP` to the Tailscale IP of the server (or `0.0.0.0` if the host firewall already restricts access) **and** add `100.64.0.0/10` to `LAN_SUBNETS`. Put your home subnet in `LAN_SUBNETS` too (e.g. `192.168.1.0/24,100.64.0.0/10`): Plex uses that list as its *LAN Networks*, so clients at home and on Tailscale count as local.

## 3. Create folders (do this before the first start)

If Docker creates the folders itself they end up owned by root and the apps cannot write to them. `services/` lives inside the repository folder when `BASE_DIR` is the repository (it is in `.gitignore`, so it never reaches git).

```bash
set -a; . ./.env; set +a
mkdir -p "$BASE_DIR"/services/{gluetun,prowlarr,sonarr,radarr,qbittorrent,recyclarr,homepage,plex}
mkdir -p "$DATA_DIR"/data/{downloads/{complete,incomplete},media/{tv-shows,movies}}
sudo chown -R "$PUID:$PGID" "$BASE_DIR/services" "$DATA_DIR/data"
```

On a server that already has a big media library, skip the recursive `chown` of `$DATA_DIR/data` (it can take a long time and the files are already yours): `chown "$PUID:$PGID"` the new `downloads/complete` and `downloads/incomplete` folders only.

Resulting layout (follows [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/) so Sonarr/Radarr can hardlink and move instantly):

```
$DATA_DIR/data
├── downloads      <- qBittorrent writes here (it cannot see anything else)
│   ├── incomplete <- unfinished torrents (files carry a .!qB extension)
│   └── complete   <- finished torrents: Sonarr/Radarr import from here
└── media
    ├── tv-shows   <- Sonarr root folder
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

> **Shortcut for steps 5 and 6: `./scripts/setup.sh`.** It starts the VPN, waits until it is healthy, starts everything else and then configures all the apps from `config/arr.yml` (see 6.0) without you opening a single web UI. Safe to run again. The manual steps below are the same thing written out.

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Wait ~30 seconds. `gluetun` must be `healthy`; the other VPN services start only after that. Then verify:

```bash
./scripts/leak_test.sh
```

Every line must start with `OK`: each service that searches or downloads has no network of its own (only gluetun's tunnel), the exit IP is a Mullvad server and not your home IP, DNS goes through gluetun, and qBittorrent is bound to `tun0`. The script exits non-zero if anything is wrong. Run it again after every change to `compose.yml`.

If `gluetun` is not healthy, see [Troubleshooting](#troubleshooting).

## 6. Configure the apps

Everything is configured **from files**, without opening the UIs: `config/arr.yml` describes the desired state of qBittorrent, Sonarr, Radarr, Prowlarr (indexers included) and Plex, and `./scripts/setup.sh` (or `./scripts/apply_arr_config.py` alone) applies it. Sections 6.1–6.4 describe the same settings by hand, for reference or if you prefer the UIs.

### 6.0 Automatic setup (recommended)

1. In `.env` choose the logins (see the table in step 2): `ARR_USERNAME`, `ARR_PASSWORD`, `QBITTORRENT_USERNAME`, `QBITTORRENT_PASSWORD`. Leave the API key variables and `PLEX_TOKEN` empty: the script fills them in.
2. **Plex, first time only:** if this Plex has never been linked to your account, generate a code at https://www.plex.tv/claim/ (valid 4 minutes) and put it in `PLEX_CLAIM` right before the first start. That is a login with your Plex account, so no script can do it for you. If Plex was already linked (for example it kept its old configuration in `$BASE_DIR/services/plex`), skip this.
3. Run:

   ```bash
   ./scripts/setup.sh
   ```

`./scripts/setup.sh` does, in this order (each step only changes what differs, so run it again whenever you like):

| Step | Effect |
|---|---|
| Folders | Creates `downloads/complete` and `downloads/incomplete` if missing |
| VPN | Starts Gluetun and waits until it is healthy (if it cannot connect it prints the log and stops) |
| Start | Starts every other container |
| `apply_arr_config.py` | See the table below |
| Recreate | Containers that read the new API keys (Homepage, Recyclarr) |
| Recyclarr | Syncs the TRaSH quality profiles, custom formats and size limits |
| `apply_arr_config.py` again | Puts the Recyclarr quality profiles on the titles that have **no file yet** (see below) |
| `apply_download_safety.py` | File filters (executables) and size ceiling |
| `leak_test.sh` | Verifies that everything that searches or downloads goes through Mullvad (see the security model) |
| `check_hardlinks.sh` | Diagnostic: are hardlinks really in place, and which finished downloads are not linked to the library (see below) |

What `apply_arr_config.py` does (`--check` shows it without changing anything):

| Service | Effect |
|---|---|
| API keys | Reads the Sonarr, Radarr and Prowlarr keys from their `config.xml` and the Plex token from `Preferences.xml`, and writes them into `.env` if empty (Homepage and Recyclarr use them) |
| qBittorrent | Signs in (on the first start with the temporary password from `docker logs`), sets your web UI login, finished downloads in `/data/downloads/complete` and unfinished ones in `/data/downloads/incomplete` (partial files get a `.!qB` extension), UPnP off, network interface `tun0`, **deletes a torrent and its files 3 days after it finished seeding** (the library keeps its copy: hardlinks), and generates an API key into `.env` |
| Sonarr / Radarr | Forms login (always required), hardlinks on, root folders, qBittorrent download client with category `tv` / `movies` (the app tests the connection before saving), removes the old Deluge client, Sonarr also gets a release profile that **rejects releases published before the episode aired**, puts the Recyclarr quality profile on the titles that have no file yet |
| Prowlarr | Forms login, FlareSolverr proxy with tag `flaresolverr`, **the indexers listed in `config/arr.yml`** (Prowlarr tests each one), links to Sonarr and Radarr (full sync, so the indexers reach both), removes indexers whose definition Prowlarr no longer has (switch: `remove_orphaned_indexers`), sets the **minimum seeders** (`minimum_seeders`, 5) that Sonarr/Radarr require |
| Plex | Turns *Remote Access* on with a manually forwarded port (32400), sets *LAN Networks* (your `LAN_SUBNETS`), adds the URLs of `PLEX_CUSTOM_URLS` if you set it, transcodes in RAM (`/transcode`) with hardware acceleration on, creates the *Movies* and *TV Shows* libraries |

To change anything, edit `config/arr.yml` and run the script again. Passwords and keys stay in `.env`: `config/arr.yml` only contains `${VAR}` placeholders. If a step reports a problem it says what to fix and carries on with the rest; the exit code is 1 if anything failed. If a login does not seem to apply, `docker compose restart sonarr radarr prowlarr`.

Notes:
- **Indexers:** `config/arr.yml` ships with a starter list of public trackers (YTS, EZTV, The Pirate Bay, 1337x). Edit it: the name is the *definition* in Prowlarr's indexer list. Private trackers need credentials: put them in `.env` and reference them from `config/arr.yml` (an example is in the file). An indexer whose site is down is reported as a problem and retried on the next run. The tag `flaresolverr` on an indexer means "solve Cloudflare with FlareSolverr".
- **Plex** is the least predictable part, because its web API is not versioned like the *arr ones. The script tries the known variants for creating a library and prints what failed. Settings this Plex does not have are skipped. Hardware transcoding needs Plex Pass and a working `/dev/dri` (see 6.4, point 4): the script only flips the switch.
- **First qBittorrent start:** if you did not set `QBITTORRENT_PASSWORD` yet, the script signs in with the temporary password and asks you to set one in `.env` and run again (it does not create the download clients until then).
- **Changing a password later:** the apps hide stored passwords and keys, so the script cannot compare them. Change it in `.env` and run `./scripts/apply_arr_config.py --force`.
- **Which quality gets downloaded is decided by the quality profile of *that title*.** Recyclarr creates the ladder profiles (*Movies - best available*, *TV - best available*), but a title keeps whatever profile it already has, and an old profile that allows everything will happily pick a 50 GB remux. That is why the script (`assign_to_existing: without_files` in `config/arr.yml`) puts the Recyclarr profile on every title that **has no file yet**: nothing exists to be replaced, so it is safe, and it also covers movies that are missing or downloading. Titles that already have a file are left alone on purpose: a file whose quality is not in the profile counts as "upgradable" and any allowed release can replace it (the ladders contain no remux, and Sonarr's stops at 1080p, so `true`, which moves every title, would let your remux and 4K series files be replaced by smaller ones). `false` disables it. Titles you add later start with the profile selected in the app's *Add* form; the script catches them the next time it runs (see the cron line in section 7).

### Open the Plex port on your router

Plex is the only service meant to be reached from the internet. The script switches Plex's *Remote Access* on, but only your router can let the traffic in, so this one step is yours (once):

1. Give the server a **fixed LAN address** (a DHCP reservation in the router): the forward points to it and breaks if the address changes.
2. Create a **port forward**: protocol **TCP**, external port **32400**, to the server's LAN address, internal port **32400**.
   - **FRITZ!Box:** *Internet → Freigaben → Portfreigaben → Gerät für Freigaben hinzufügen* (choose the server) *→ Neue Freigabe → Portfreigabe*, TCP, 32400 to 32400 (menu names as in the German interface; they can differ slightly between firmware versions).
   - **Other routers:** look for *Port forwarding*, *Virtual server* or *NAT*.
3. In Plex, *Settings → Remote Access* must say **Fully accessible outside your network** (it can take a minute). Test it from outside too, for example with your phone on mobile data.
4. If it does not work: [Troubleshooting](#troubleshooting) (double NAT, provider CGNAT, wrong address).

Do not forward any other port: everything else stays private. You do not need UPnP (and it cannot work from inside Docker anyway).

### 6.1 qBittorrent by hand — `http://<LAN_IP>:8080`

*(done by `apply_arr_config.py`)*

1. Get the temporary password: `docker logs qbittorrent 2>&1 | grep -i "temporary password"`. Log in as `admin`.
2. **Tools → Options → Web UI**: set your own username and password.
3. **Downloads**: *Default Save Path* = `/data/downloads/complete`; tick *Keep incomplete torrents in* = `/data/downloads/incomplete`; tick *Append .!qB extension to incomplete files*.
4. **Connection**: untick *Use UPnP / NAT-PMP port forwarding* (useless behind Mullvad).
5. **Advanced → Network Interface**: select **`tun0`** (the VPN interface inside Gluetun, also used for WireGuard). This is a second layer on top of the kill switch: qBittorrent will refuse to use any other interface.
6. Save.

### 6.2 Prowlarr by hand — `http://<LAN_IP>:9696`

*(login, FlareSolverr and the indexers of `config/arr.yml` are done by the script)*

1. On first login set authentication to *Forms* and choose a username/password (**Settings → General → Authentication**, *Required*).
2. **Settings → Indexers → +** *FlareSolverr*: Host `http://localhost:8191`, Tags `flaresolverr`. Give that tag to the indexers that need Cloudflare solving.
3. **Indexers → Add Indexer**: add your indexers (only needed for ones not listed in `config/arr.yml`).
4. **Settings → Apps**: add Sonarr and Radarr later, after step 6.3 (you need their API keys).

### 6.3 Sonarr and Radarr by hand — `http://<LAN_IP>:8989`, `http://<LAN_IP>:7878`

*(done by `apply_arr_config.py`)*

For each one:

1. Set authentication (*Forms*, *Required*) on first login.
2. **Settings → Media Management → Root Folders**: Sonarr `/data/media/tv-shows`, Radarr `/data/media/movies`. Keep *Use Hardlinks instead of Copy* enabled.
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

### 6.4 Plex — `http://<server address>:32400/web`

*(libraries and network settings are done by the script; what remains is the account link, the router port forward and hardware transcoding)*

1. Because of `PLEX_CLAIM` the server is attached to your Plex account automatically. If you skipped it (or it expired), run `docker compose up -d --force-recreate plex` with a fresh token.
2. Libraries. Plex mounts `$DATA_DIR` read-only as `/data/media`, so the folders are:
   - TV: `/data/media/data/media/tv-shows`
   - Movies: `/data/media/data/media/movies`
3. **Settings → Network**: *LAN Networks* = your `LAN_SUBNETS` (add your home subnet there); *Custom server access URLs* = `PLEX_CUSTOM_URLS` if you set it (Tailscale address, a DDNS name...). Plex runs in bridge mode, so without the LAN Networks, clients on your own network can be treated as remote.
4. **Transcoding:** the script points the transcoder's temporary folder at the 6 GB RAM disk of `compose.yml` and switches hardware acceleration on. Software (CPU) transcoding always works. Hardware transcoding also needs **Plex Pass**, an Intel GPU exposed as `/dev/dri` (already mapped) and the permission to use it inside the container. To check: play something that must be transcoded and open Plex's *Dashboard*: it must say *Transcode (hw)*. If it says only *Transcode*, run `docker exec plex ls -l /dev/dri` and `docker exec plex id abc` (the user needs the group of `renderD128`); as a test, remove `cap_drop` for `plex` in `compose.yml`.
5. **Remote access** is switched on by the script, with the port set manually because UPnP cannot work from inside Docker. The router side is up to you: [Open the Plex port on your router](#open-the-plex-port-on-your-router). Then *Settings → Remote Access* should say *Fully accessible outside your network*.

Plex needs write access only to its own config, so the media is mounted `:ro`. If you enable *Allow media deletion* in Plex, remove the `:ro` from the `plex` volume.

Hardening for a server that faces the internet, in Plex itself: turn on two-factor authentication on your Plex account, leave *List of IP addresses and networks that are allowed without auth* empty, do not forward any other port, and keep the image current (Renovate proposes Plex updates as soon as a release is a day old).

### 6.5 Homepage — `http://<LAN_IP>`

*(the credentials it needs are filled in by the script; nothing else to do)*

The dashboard is configured **as code**: `config/homepage/settings.yaml`, `services.yaml` and `widgets.yaml` live in this repo and are mounted read-only, so the container cannot change them; a change is a `git commit` plus `docker compose restart homepage`. Homepage also insists on a few other, empty files (bookmarks, docker, ...): it creates them itself in `$BASE_DIR/services/homepage`, outside the repo. It has no Docker socket. You log in with `HOMEPAGE_AUTH_PASSWORD`.

The links and status dots work immediately. The widgets (download queue, Plex streams, ...) need the credentials below in `.env`. `./scripts/apply_arr_config.py` (6.0) has already filled all of them, `PLEX_TOKEN` included. Then recreate the container:

| `.env` variable | Where to find it |
|---|---|
| `SONARR_API_KEY`, `RADARR_API_KEY`, `PROWLARR_API_KEY` | each app → **Settings → General → API Key** |
| `QBITTORRENT_API_KEY` | qBittorrent → **Tools → Options → Web UI → Authentication → API Key** (generate one; needs qBittorrent ≥ 5.2) |
| `PLEX_TOKEN` | Plex web → open any item → **⋯ → Get Info → View XML**; copy `X-Plex-Token=...` from the URL ([details](https://www.plexopedia.com/plex-media-server/general/plex-token/)) |

```bash
docker compose up -d homepage
```

Add or reorder services in `config/homepage/services.yaml`. Anything written as `{{HOMEPAGE_VAR_NAME}}` in those files is replaced with the `HOMEPAGE_VAR_NAME` environment variable of the container (see the `homepage` service in `compose.yml`), so no secret ever ends up in git. Use `gluetun:<port>` for services behind the VPN and `plex:32400` for Plex when you configure widgets (Homepage reaches them over the Docker networks).

### 6.6 Recyclarr and download safety by hand

*(done by `setup.sh`; use this to run the two steps on their own)*

Both need the Sonarr/Radarr (and qBittorrent) API keys, which `apply_arr_config.py` (6.0) puts in `.env`. Recyclarr does not sync when it starts: it only runs at its schedule (daily at 00:00 UTC), so the first sync has to be started by hand:

```bash
docker compose up -d                                       # picks up the new keys (homepage, recyclarr)
docker compose exec recyclarr recyclarr sync --preview     # what Recyclarr would change
docker compose exec recyclarr recyclarr sync               # first sync, now (then it repeats daily by itself)
./scripts/apply_download_safety.py --check                 # what the file filters would change
./scripts/apply_download_safety.py                         # apply them
```

- **Recyclarr** reads `config/recyclarr/configs/media.yml` (in this repo, mounted read-only). It also sets the per-quality **size limits** ([File size](#file-size-what-is-configured)). It creates the quality profiles **Movies - best available** (Radarr) and **TV - best available** (Sonarr): TRaSH's profiles with their custom formats and scores, renamed and with the quality ladder of [How a release is chosen](#how-a-release-is-chosen-and-what-to-do-when-a-download-crawls). It does not touch your existing profiles or titles (see the warning in 6.0). For 2160p, remux or anime, take another template from https://github.com/recyclarr/config-templates and put it in `config/recyclarr/configs/`.
- **`apply_download_safety.py`** (file filters and the Radarr size ceiling) is idempotent: run it again whenever Prowlarr adds indexers to Sonarr/Radarr (the setting it enforces is per indexer, see below).

## Fake releases, executables and archives: how downloads are controlled

Torrent sites are full of fake releases: an `.exe` "codec", an archive with a password, a tiny file named after an episode that has not aired yet. They are stopped in layers, from the earliest to the last. Most of the work is done **before anything is downloaded**:

| # | Where | What it does |
|---|---|---|
| 1 | **Quality profile + Recyclarr** | Only the qualities of the profile (no CAM or telesync). TRaSH custom formats score down BR-DISK, LQ, fake and low-quality release groups |
| 2 | **Sonarr release profile: "Reject Unaired Releases"** (`sonarr.release_profile` in `config/arr.yml`) | Refuses any release **published before the episode aired**: this is the fake "new episode" that appears the day before. `grace_days: 0` means "at or after the air date". An episode without a known air date is refused too |
| 3 | **Size limits** (Recyclarr) | Below the minimum MB per minute a release is rejected: a 3 MB "episode" never passes (a 45-minute WEB-DL 1080p must be at least ~700 MB) |
| 4 | **Minimum seeders** (`minimum_seeders`, 5) | Rejects torrents nobody is sharing |
| 5 | **Fail Downloads, per indexer** (set by `apply_download_safety.py`) | Reads the file list inside the `.torrent` **before** sending it to qBittorrent. A release with `.exe .bat .cmd .sh`, "potentially dangerous" files (`.lnk .scr .ps1 .vbs .arj .lzh .zipx`) or, in Sonarr, your extra extensions (`.msi .js .jar .dll .apk ...`) is rejected, blocklisted, and the next best release is tried. Look for *"Caution: Found executable..."* in Activity/History: that is it working |
| 6 | **qBittorrent, *Excluded file names*** (set by `apply_download_safety.py`) | Whatever slips through (magnet links have no file list to inspect) is never written to disk if it matches `*.exe *.msi *.bat *.scr ...`. External-program hooks are disabled |
| 7 | **Import** | Only files with a video extension are ever moved into the library. Everything else stays in `downloads/complete` and is deleted with the torrent after 3 days |
| 8 | *(optional)* **`noexec` on the data disk** | In `/etc/fstab` add `noexec,nosuid,nodev` to the mount options of the disk that holds `DATA_DIR`: nothing stored there can be executed, whatever it is |

**Archives.** Some genuine releases (movies and series alike) are shipped as `.rar`, `.zip` or `.7z`. Sonarr and Radarr refuse to import them ("Found archive file, might need to be extracted") and qBittorrent does not extract, so without help the download is wasted. **Unpackerr** (a container in the VPN group) extracts the archives *of downloads that Sonarr/Radarr grabbed*, the extracted video is imported by hardlink like any other, and the extracted copy is removed five minutes later; the archive stays until the torrent is deleted. What is inside is then subject to the same rule as everything else: only video files reach the library. Archives are deliberately **not** put on any reject list: Sonarr does not even allow it, and it would throw away legitimate releases.

Honest limits:
- **Magnet links** have no file list to inspect before the download: they rely on layers 1–4 and 6.
- **Radarr has no air-date rule** (only Sonarr does). For movies the protection is the quality profile (no CAM/telesync), the size minimum, the seeders and *Minimum Availability*.
- A file that is **not a video but has a video name** would be imported: nothing checks the *content* any more. Extracting an archive writes its content to disk (nothing is executed); `noexec` (layer 8) makes that harmless.
- Extension and container checks do not detect a *valid* video file crafted to exploit a player. Keep Plex and your players updated.

## File size: what is configured

Radarr and Sonarr judge a release by **MB per minute of runtime, per quality** (a 120-minute film at 60 MB/min is 7.2 GB). A release above `max` is rejected, one below `min` too, and among acceptable ones the closest to `preferred` wins. TRaSH's default is "max 2000, preferred 1999", i.e. no limit and the biggest file wins, which is how 50 GB remuxes get picked. `config/recyclarr/configs/media.yml` overrides it:

| Quality | Radarr: preferred / max (120 min film) | Sonarr: preferred / max (45 min episode) |
|---|---|---|
| WEB-DL / WEBRip 1080p | 3.6 GB / 7.2 GB | 1.1 GB / 2.7 GB |
| Bluray 1080p | 8.4 GB / 13 GB (min 2.4 GB) | 2.7 GB / 4.5 GB (min 1.1 GB) |
| WEB-DL / WEBRip 720p | 1.8 GB / 4.8 GB (min 0.7 GB) | 0.5 GB / 1.4 GB (min 0.2 GB) |
| Bluray 720p | 3.6 GB / 7.2 GB (min 1 GB) | 1.1 GB / 2.3 GB (min 0.4 GB) |
| WEB-DL / WEBRip 2160p | 12 GB / 19 GB | 2.7 GB / 6.8 GB |
| Bluray 2160p | 18 GB / 24 GB (min 7 GB) | 5.4 GB / 9 GB |
| Remux (1080p and 2160p) | never fits (cap ≈ minimum) | never fits |
| 480p (WEB, Bluray, DVD, SDTV) | not managed by TRaSH: the apps' own defaults apply | same |

The minimums of the fallback qualities are lower than TRaSH's on purpose (Bluray 1080p 20 instead of 50.8 MB/min, 720p 5–8 instead of 10–25), so that the small releases which really exist can be chosen when the big ones lack seeders. Fakes stay far below any minimum.

What you can turn, from the biggest effect to the smallest:

1. **Which qualities are allowed at all, and in which order** (`qualities` of each profile in `config/recyclarr/configs/media.yml`). Movies: Bluray/4K down to 480p, no remux. Series: 1080p down to 480p, no 4K. To drop a rung, remove it from the list (mind that a quality missing from a profile counts as "upgradable" for files already having it).
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

## Clean-up: finished downloads are deleted after 3 days, the library keeps its files

qBittorrent deletes a torrent **and its files** by itself once it has **seeded for 3 days after finishing** (`cleanup: delete_after_seeding_days: 3` in `config/arr.yml`; the time counts from completion, not from when you added it). Plex keeps its copy because of **hardlinks**: when Sonarr/Radarr import a file, `media/...` gets a second name for the *same data* as `downloads/complete/...` instead of a copy. Deleting one name leaves the data reachable through the other, so what Plex plays stays. It also means seeding costs no extra disk space.

Hardlinks only work when `downloads/complete` and `media/` are on the same filesystem (both live under `$DATA_DIR/data` and Sonarr/Radarr see them as one mount, `/data`): otherwise imports silently become copies (still safe for Plex, only wasteful).

**The one thing that can be lost: a download that was never imported.** If Sonarr/Radarr could not import a finished download (unknown series, "import blocked" in *Activity → Queue*), the only copy is the one in `downloads/complete`, and qBittorrent deletes it after 3 days like the others. Look at the queue in Sonarr/Radarr from time to time. `./scripts/check_hardlinks.sh` lists them; **nobody runs it for you**: `setup.sh` runs it once, so run it yourself now and then (or from cron). It checks that Sonarr/Radarr have hardlinks on, that the two folders share a filesystem, which finished downloads are *not* hardlinked into the library (never imported, or imported as a copy), and which recent library files were copied. Archives are counted apart (Unpackerr extracts them, so the archive itself is never linked).

Tuning, all in `config/arr.yml`, then `./scripts/apply_arr_config.py`:

- **Another delay:** `delete_after_seeding_days` (fractions are fine: `0.5` is 12 hours; `0` = never clean up).
- **Keep the files, drop the torrent:** `delete_files: false`.
- **Safer variant:** `only_after_import: true` makes qBittorrent only *stop* the torrent after the delay, and lets Sonarr/Radarr delete it (files included) only if *they* imported it: a download that could not be imported is then never deleted (it stays on disk, stopped).
- **Private trackers:** they want a minimum ratio or seeding time. Do not use a short delay for them.

## How a release is chosen, and what to do when a download crawls

Sonarr and Radarr first drop the releases that fail the rules (wrong quality, outside the size limits, too few seeders, executables...), then rank the rest. The first rule that tells two releases apart decides:

| # | Rule | Notes |
|---|---|---|
| 1 | **Quality** | Position in the quality profile (higher wins), then Proper/Repack |
| 2 | **Custom format score** | TRaSH scores: reliable release groups, penalties for LQ and fake releases |
| 3 | Indexer priority | One number per indexer in Prowlarr (Sonarr also prefers season packs here) |
| 4 | **Seeders**, then peers | On a logarithmic scale: 1–9, 10–99 and 100+ seeders are three bands, 20 and 90 are equal |
| 5 | **Size** | Closest to the `preferred` size of `config/recyclarr/configs/media.yml`, in bands of 200 MB |

**The quality ladder.** Because *quality comes first*, the profile is an ordered list and the search walks down it. A release is only a candidate if it has enough seeders (`minimum_seeders`), so "best quality with good seeds, otherwise the next one down" is exactly what happens:

| | Order (best first) | Stops upgrading at |
|---|---|---|
| **Movies** (Radarr, *Movies - best available*) | Bluray 2160p → WEB 2160p → Bluray 1080p → WEB 1080p → Bluray 720p → WEB 720p → WEB 480p → Bluray 480p → DVD | Bluray 2160p |
| **Series** (Sonarr, *TV - best available*) | WEB 1080p → Bluray 1080p → HDTV 1080p → WEB 720p → Bluray 720p → HDTV 720p → WEB 480p → Bluray 480p → DVD → SDTV | WEB 1080p |

Nothing is refused for being "too low" as long as it is on the list and has seeders: a 720p with 60 seeders is taken over a 1080p with 2. If a better quality shows up later, the title is upgraded until the cutoff. Change the order or remove rungs in `config/recyclarr/configs/media.yml`, then `docker compose exec recyclarr recyclarr sync`. Titles that already have a profile keep it (see 6.0); the ladder applies to titles without a file and to new ones.

So seeders never outweigh quality: a better release with 2 seeders beats a slightly worse one with 500. What protects you from dead torrents is the **minimum seeders** rule (`minimum_seeders` in `config/arr.yml`, default 5): a release below it is rejected and the next best one is used. It is set once in Prowlarr's app profile and reaches every indexer (an indexer with its own minimum keeps it). The first acceptable release is grabbed immediately; if a better one shows up later and the quality cutoff is not reached yet, it is upgraded.

**A download is slow: find out why.**

```bash
set -a; . ./.env; set +a
# every torrent: state, speed, connected seeders / seeders in the swarm, availability, progress
curl -s -H "Authorization: Bearer $QBITTORRENT_API_KEY" "http://$LAN_IP:8080/api/v2/torrents/info" | python3 -c "
import sys,json
for t in json.load(sys.stdin):
    print('%-42.42s %-14s %6.2f MB/s  seed %s/%s  avail %.2f  %3.0f%%' % (t['name'], t['state'], t['dlspeed']/1e6, t['num_seeds'], t['num_complete'], t['availability'], t['progress']*100))"
# speed of the VPN itself (Sonarr shares Gluetun's network)
docker exec sonarr curl -s -o /dev/null -w "%{speed_download} bytes/s\n" https://proof.ovh.net/files/100Mb.dat
```

- **Few seeders (`seed 0/4`) or availability below 1:** the torrent is poor, nothing on your side can fix it. Drop it and let Radarr/Sonarr pick another (below).
- **State `queuedDL`:** qBittorrent runs at most 3 downloads at a time; the others wait.
- **The VPN test is slow:** try another exit in `VPN_COUNTRIES` (comma separated list allowed). Mullvad no longer offers port forwarding, so only peers you can reach connect to you, which hurts poorly seeded torrents most.
- **`dl_limit` not 0 or a scheduler on** in qBittorrent: check `GET /api/v2/app/preferences`.

Drop a stuck release, blocklist it and search again (Radarr; Sonarr uses port 8989 and its own key):

```bash
curl -s -H "X-Api-Key: $RADARR_API_KEY" "http://$LAN_IP:7878/api/v3/queue" | python3 -c "
import sys,json
for r in json.load(sys.stdin)['records']: print(r['id'], r['title'])"
curl -s -X DELETE -H "X-Api-Key: $RADARR_API_KEY" "http://$LAN_IP:7878/api/v3/queue/ID?removeFromClient=true&blocklist=true"
```

## 7. Day to day

```bash
docker compose ps                                # status
docker compose logs -f gluetun                   # VPN logs
./scripts/leak_test.sh                           # does everything that downloads go through the VPN? (--kill-switch: also cut the tunnel)
./scripts/check_hardlinks.sh                     # are hardlinks in place, which finished downloads are not linked to the library? (nobody runs it for you)
./scripts/setup.sh                               # bring everything to the state described in config/arr.yml (safe to repeat)
./scripts/apply_arr_config.py --check            # is the *arr setup still what config/arr.yml says? (changes nothing)
./scripts/apply_download_safety.py               # re-apply file filters (after adding indexers)
```

To make titles you add later follow the 1080p profile without thinking about it, run the configuration script regularly (it only changes what differs, and it is quiet when nothing does):

```bash
# crontab -e
*/30 * * * * cd /path/to/media-server && ./scripts/apply_arr_config.py >> /tmp/apply_arr_config.log 2>&1
```

**Updates: Renovate opens a pull request, you merge it.** Images are pinned to exact versions (no auto-updater, no Docker socket for anybody). [Renovate](https://github.com/apps/renovate) watches `compose.yml` and proposes new versions as pull requests; nothing changes on the server until you merge one and pull it.

One-time setup:

1. Commit and push this repo to GitHub (Renovate reads `renovate.json` from the default branch).
2. Install the free **Renovate** GitHub App (https://github.com/apps/renovate) and give it access to this repository only.
3. Within minutes it opens a **"Pin Docker digests"** PR: it adds `@sha256:...` next to every tag, so a tag that is moved after publication can never silently give you a different image. Merge it once, then deploy (below).

How it behaves (`renovate.json`):

- **Mondays before 08:00** (Europe/Amsterdam) it opens or refreshes its PRs; the rest of the week it stays quiet. The exception is **Plex**, which can face the internet: its updates are proposed at any time, one day after release.
- **One PR per image, always the newest version.** If a newer release appears while a PR is still open, that same PR is updated to the newer version (branch and title), so you never have an old and a new PR for the same image. If you close a PR without merging, it will not come back for that version, only for a newer one. There is also a *Dependency Dashboard* issue on GitHub listing everything pending.
- **3-day cool-down**: a release is proposed only when it is at least 3 days old, which is when a broken or compromised release is usually pulled or fixed. Rebuilds of the linuxserver images (new base-image security patches, same app version) count as updates too.
- **Never auto-merges.** Read the PR (release notes are linked), merge it from the GitHub app on your phone.

Deploy after merging:

```bash
git pull && ./scripts/setup.sh
```

Something broke? `git revert <merge commit>`, then the same command.

**Backup**: `$BASE_DIR/services` (all app databases and configs: libraries, watch history, API keys), the Mullvad key file and your `.env` (passwords and tokens: keep the copy encrypted). Media does not need the containers. Nothing here is backed up automatically.

## Security model

- Only Plex and Homepage are outside the VPN, and only Plex is meant to be reachable from the internet (behind your router's port forward, protected by Plex accounts). Everything that searches or downloads shares Gluetun's network namespace and its firewall (kill switch, DNS over TLS through the tunnel).
- qBittorrent is also bound to `tun0`.
- The web UIs are published only on `LAN_IP`, never on `0.0.0.0` unless you set it. This matters because Docker-published ports bypass ufw/firewalld. **Plex is the exception on purpose**: it is published on every IPv4 address (TCP 32400 only) so you can forward it from your router.
- FlareSolverr (no authentication) is not published at all.
- No container has the Docker socket.
- Plex and the VPN group are on separate Docker networks, so a compromised Plex cannot reach the downloader or the apps. Homepage is the only container on both (it needs to query both), with a read-only config and a login. Plex sees your media read-only.
- All containers use `no-new-privileges`; every container except Gluetun also uses `cap_drop: ALL` (only the capabilities needed by the linuxserver init are added back). If a container refuses to start, remove `cap_drop` for that service and check `docker compose logs <service>`.
- The Mullvad key is a Docker secret and `.env` is git-ignored.
- **`./scripts/leak_test.sh` proves the VPN protection instead of assuming it.** It checks that every running container, except `gluetun`, `plex` and `homepage`, lives inside gluetun's network (so a service you add later without `network_mode: service:gluetun`, a second torrent client for example, is flagged as a leak *before* it matters), that the exit IP is a Mullvad server and not your home IP, that DNS goes through gluetun, and that qBittorrent is bound to `tun0`. `--kill-switch` also takes the tunnel down for a moment and checks that the torrent client is left with no connectivity at all (downloads pause for about 30 seconds). Run it after every change to `compose.yml`; `setup.sh` runs it too. What a VPN does not hide: your ISP sees that you use Mullvad (not what you download), Plex knows your library titles and watch history through your Plex account, and accounts on private trackers are yours.
- **Tailscale ACLs:** everything on your tailnet that can reach the server can reach these UIs. In the Tailscale admin console, tag the server and allow only your own devices to its ports (80, 8080, 8989, 7878, 9696, 32400), so a compromised device on the tailnet cannot reach them all.
- Every web UI has a login (set from `ARR_*` / `QBITTORRENT_*` / `HOMEPAGE_AUTH_PASSWORD` in `.env`). The apps run inside the VPN but they are still reachable by anyone who can reach `LAN_IP`.
- `services/` (each app's database and API keys) and `.env` are git-ignored: never commit them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `required variable LAN_IP is missing` | Fill `LAN_IP` and `LAN_SUBNETS` in `.env` |
| `gluetun` stays `unhealthy` / restarts | `docker compose logs gluetun`. Check: private key file has no trailing newline or spaces; `WIREGUARD_ADDRESSES` is the IPv4 `/32` only; the key is registered in your Mullvad account (generated on the site) and you are within 5 devices; `VPN_COUNTRIES` is a valid English country name; no other client uses the same key |
| `/dev/net/tun: no such file` | `sudo modprobe tun` (persist with `echo tun \| sudo tee /etc/modules-load.d/tun.conf`) |
| After a reboot the web UIs are not up although `docker compose ps` looks fine, and `LAN_IP` is the Tailscale address | Docker started before Tailscale, so the address did not exist yet. `sudo systemctl edit docker` and add `[Unit]`, `After=tailscaled.service`, `Wants=tailscaled.service`; then reboot once and check `docker compose ps` |
| `cannot assign requested address` / port bind errors | `LAN_IP` is not an address of this host, or the port (80, 8080, 32400, ...) is already used by something else |
| UIs not reachable from your PC | Your PC's subnet must be in `LAN_SUBNETS`, and you must use `http://<LAN_IP>:<port>` |
| Sonarr/Radarr cannot reach qBittorrent or Prowlarr | Use `localhost`, not container names (they share a network namespace) |
| `Permission denied` in the apps | Re-run the `chown` in step 3; `PUID`/`PGID` must match the folder owner |
| A linuxserver container exits right after start | Remove `cap_drop: ALL` for that service and read its logs |
| Prowlarr indexers fail with Cloudflare errors | The indexer needs the `flaresolverr` tag (6.2). Some indexers also block Mullvad IPs: try another `VPN_COUNTRIES` |
| Sonarr/Radarr metadata (TVDB/TMDB) errors | Rarely, a service blocks VPN IPs. Try another `VPN_COUNTRIES`; as a last resort Sonarr/Radarr can be moved out of the VPN (this lowers protection, so keep indexers only in Prowlarr) |
| Homepage: `Host validation failed` | The address you type in the browser must be in `HOMEPAGE_ALLOWED_HOSTS` (default: `LAN_IP`). Add other names with `HOMEPAGE_ALLOWED_HOSTS=192.168.1.10,server.tailnet.ts.net` in `.env` |
| Homepage widgets show errors or no numbers | The matching key in `.env` is empty or wrong (6.5); run `docker compose up -d homepage` after editing `.env` |
| `setup.sh` stops at *gluetun is 'starting'* | The VPN does not connect: Mullvad key and address must be a matching pair (step 4) |
| Prowlarr health: *Indexers have no definition and will not work: ...* | Those indexers come from an older Prowlarr and their site definition was removed upstream (usually because the site closed), so they cannot work again; the warning stays as long as they exist. `./scripts/apply_arr_config.py` deletes them (`remove_orphaned_indexers: true` in `config/arr.yml`; it checks first that Prowlarr's list of definitions is complete). To keep them, set it to `false`. If you have the definition file from somewhere else (a Cardigann `.yml`, for example from Jackett), put it in `$BASE_DIR/services/prowlarr/Definitions/Custom/` and restart Prowlarr |
| `apply_arr_config.py`: `indexer ...: Unable to connect` | The tracker's site is down or blocks the exit IP (or needs FlareSolverr: give it `tags: [flaresolverr]`). It is retried on every run; try another `VPN_COUNTRIES` or remove it from `config/arr.yml` |
| `apply_arr_config.py`: Plex `not linked to a Plex account` | Put a fresh `PLEX_CLAIM` in `.env`, `docker compose up -d --force-recreate plex`, run `./scripts/setup.sh` again |
| `apply_arr_config.py`: Plex `library ...: HTTP 4xx` | Plex rejected every known way of creating a library. Create it once by hand (Plex web UI) and the script leaves it alone from then on |
| `apply_arr_config.py`: `PyYAML is required` | `sudo apt install python3-yaml` |
| `apply_arr_config.py`: `cannot sign in to qBittorrent` | `QBITTORRENT_USERNAME`/`QBITTORRENT_PASSWORD` in `.env` are used as the credentials qBittorrent has *now*. To set a new password from scratch: `docker compose stop qbittorrent`, then `sed -i '/^WebUI\\Password_PBKDF2/d' "$BASE_DIR/services/qbittorrent/qBittorrent/qBittorrent.conf"`, then `docker compose start qbittorrent`, and run the script again: it signs in with the temporary password from `docker logs` and sets the ones from `.env`. `HTTP 403` means this address is banned after too many failed logins: `docker compose restart qbittorrent` |
| `apply_arr_config.py`: `no API key for sonarr` | The app has not started yet or its `config.xml` is not in `$BASE_DIR/services/<app>/`. Check `docker compose ps` and that `BASE_DIR` in `.env` is an absolute path |
| `apply_arr_config.py`: `root folder ...: Folder does not exist` | Create it on the host (step 3) and check ownership: `PUID:PGID` must own `$DATA_DIR/data` |
| `recyclarr sync` reports `api_key`/401/connection errors | `SONARR_API_KEY` / `RADARR_API_KEY` are empty or wrong in `.env`; fix and `docker compose up -d recyclarr` |
| `apply_download_safety.py`: `skipped ... is empty` or connection errors | Fill the API keys in `.env`; the script talks to `http://<LAN_IP>:<port>`, so run it from a machine in `LAN_SUBNETS` (the server itself is fine) |
| A release disappeared / "Caution: Found executable" in Activity | The protection worked: the release was blocklisted and another one is tried |
| Plex clients on the LAN play "remotely" | Put your home subnet in `LAN_SUBNETS` and run `./scripts/apply_arr_config.py` (it sets Plex's *LAN Networks*) |
| Plex transcodes but the Dashboard never shows *(hw)* | Hardware transcoding needs Plex Pass, `/dev/dri` and permission to use it. `ls -l /dev/dri` on the host, `docker exec plex ls -l /dev/dri` and `docker exec plex id abc` (the user needs the group of `renderD128`). Try removing `cap_drop` for `plex` in `compose.yml`. No Intel GPU at all: remove the `devices:` block of `plex` and turn `HardwareAcceleratedCodecs` off in `config/arr.yml` |
| Plex *Remote Access*: "Not available outside your network" | The router does not forward TCP 32400 to the server's LAN address, or the address changed (give the server a DHCP reservation), or your ISP uses CGNAT (your router's public IP differs from what a "what is my IP" site shows: IPv4 forwarding cannot work, ask the ISP for a public IPv4). Also check `docker compose ps plex` shows `0.0.0.0:32400->32400/tcp` |

## Migrating from the previous setup (WireGuard + Deluge + Overseerr + Watchtower)

1. Let active downloads finish, or note what is in Deluge: qBittorrent starts empty. Finished files stay in `$DATA_DIR/data/downloads`; new downloads go to `downloads/complete` (unfinished ones to `downloads/incomplete`).
2. Update the repo and follow steps 2–4 above (`.env`, folders, Mullvad key).
3. Stop the old stack with the old files (`docker compose down`) *before* starting Gluetun: only one client may use the Mullvad key at a time. Then run `./scripts/setup.sh`.
4. The script also migrates the apps: it replaces the Deluge client with qBittorrent and changes the Prowlarr/Sonarr/Radarr addresses from `wireguard` or container names to `localhost` (check with `./scripts/apply_arr_config.py --check` first).
5. You can delete `$BASE_DIR/services/{deluge,overseerr,wireguard,homarr}` when you are sure you do not need them. Requests made in Overseerr are not migrated.
