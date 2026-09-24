#!/usr/bin/env python3
"""Make qBittorrent, Sonarr and Radarr refuse anything that is not a video release.

Idempotent, standard library only. Reads LAN_IP and the API keys from .env and talks to the
UIs published on the LAN address. Run it after the first setup and again whenever Prowlarr
adds new indexers to Sonarr/Radarr (their "Fail Downloads" option is per indexer and empty
by default, which is why executables can slip through).

    ./apply_download_safety.py            apply
    ./apply_download_safety.py --check    show what would change, change nothing

What it sets
  qBittorrent  never download files matching EXCLUDED_PATTERNS; never run external programs
  Sonarr       every indexer: fail (and blocklist) releases containing executables, "potentially
               dangerous" files or USER_REJECTED extensions; auto-redownload failed releases
  Radarr       same, without the user-defined list (Radarr has no such option), plus a hard
               size ceiling per release (MAX_RELEASE_MB)
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Wildcards, case-insensitive. Harmless to over-match here: a skipped stray file costs nothing.
EXCLUDED_PATTERNS = [
    "*.exe", "*.msi", "*.bat", "*.cmd", "*.com", "*.scr", "*.pif", "*.lnk", "*.vbs", "*.vbe",
    "*.js", "*.jse", "*.wsf", "*.wsh", "*.ps1", "*.sh", "*.jar", "*.dll", "*.apk", "*.hta",
    "*.reg", "*.cpl", "*.dmg", "*.pkg", "*.iso",
]

# Sonarr "Additional Rejected File Extensions". Must not contain archive or media extensions
# (Sonarr refuses those) and avoids ".com": promo files like "www.site.com" would reject good releases.
USER_REJECTED = "msi,js,jse,jar,dll,apk,pif,hta,reg,cpl,wsf,vbe,iso,dmg,pkg"

# Hard ceiling for a single release, in MB (Settings -> Indexers -> Maximum Size). Radarr only: in Sonarr
# it would also reject season packs. Per-quality limits (Recyclarr) do the fine-grained work; this catches
# releases with a wrong/unknown quality label. 0 = unlimited. Interactive search can still force a grab.
MAX_RELEASE_MB = {"radarr": 25000}

# FailDownloads enum: 0 = Executables, 1 = Potentially Dangerous, 2 = User Defined Extensions
FAIL_DOWNLOADS = {"sonarr": [0, 1, 2], "radarr": [0, 1]}

CHECK = "--check" in sys.argv
changes = 0


def load_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.split(" #")[0].strip().strip("'\"")
    return env


def call(method, url, headers, payload=None, form=None):
    data = None
    headers = dict(headers)
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def note(message):
    global changes
    changes += 1
    print(("  would change: " if CHECK else "  changed:      ") + message)


def qbittorrent(host, key):
    print("qBittorrent")
    headers = {"Authorization": f"Bearer {key}"}
    base = f"http://{host}:8080/api/v2/app"
    current = call("GET", f"{base}/preferences", headers)
    wanted = {
        "excluded_file_names_enabled": True,
        "excluded_file_names": "\n".join(EXCLUDED_PATTERNS),
        "autorun_enabled": False,
        "autorun_on_torrent_added_enabled": False,
    }
    diff = {k: v for k, v in wanted.items() if current.get(k) != v}
    if not diff:
        print("  ok")
        return
    note(", ".join(diff))
    if not CHECK:
        call("POST", f"{base}/setPreferences", headers, form={"json": json.dumps(diff)})


def arr(name, port, host, key):
    print(name.capitalize())
    headers = {"X-Api-Key": key}
    base = f"http://{host}:{port}/api/v3"
    before = changes

    for indexer in call("GET", f"{base}/indexer", headers):
        field = next((f for f in indexer["fields"] if f["name"] == "failDownloads"), None)
        if field is None:
            continue
        wanted = FAIL_DOWNLOADS[name]
        if sorted(field.get("value") or []) != wanted:
            note(f"indexer '{indexer['name']}': failDownloads {field.get('value')} -> {wanted}")
            if not CHECK:
                field["value"] = wanted
                call("PUT", f"{base}/indexer/{indexer['id']}?forceSave=true", headers, payload=indexer)

    media = call("GET", f"{base}/config/mediamanagement", headers)
    if "userRejectedExtensions" in media and media["userRejectedExtensions"] != USER_REJECTED:
        note(f"rejected extensions -> {USER_REJECTED}")
        if not CHECK:
            media["userRejectedExtensions"] = USER_REJECTED
            call("PUT", f"{base}/config/mediamanagement/{media['id']}", headers, payload=media)

    if name in MAX_RELEASE_MB:
        indexer_cfg = call("GET", f"{base}/config/indexer", headers)
        if indexer_cfg.get("maximumSize") != MAX_RELEASE_MB[name]:
            note(f"maximum release size {indexer_cfg.get('maximumSize')} -> {MAX_RELEASE_MB[name]} MB")
            if not CHECK:
                indexer_cfg["maximumSize"] = MAX_RELEASE_MB[name]
                call("PUT", f"{base}/config/indexer/{indexer_cfg['id']}", headers, payload=indexer_cfg)

    config = call("GET", f"{base}/config/downloadclient", headers)
    wanted = {"enableCompletedDownloadHandling": True, "autoRedownloadFailed": True}
    diff = {k: v for k, v in wanted.items() if config.get(k) != v}
    if diff:
        note("download client settings: " + ", ".join(diff))
        if not CHECK:
            config.update(diff)
            call("PUT", f"{base}/config/downloadclient/{config['id']}", headers, payload=config)

    for client in call("GET", f"{base}/downloadclient", headers):
        if not client.get("removeFailedDownloads"):
            note(f"download client '{client['name']}': remove failed downloads")
            if not CHECK:
                client["removeFailedDownloads"] = True
                call("PUT", f"{base}/downloadclient/{client['id']}?forceSave=true", headers, payload=client)

    if changes == before:
        print("  ok")


def main():
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        sys.exit(".env not found (run this from the repository, after creating .env)")
    env = load_env(env_file)
    host = env.get("LAN_IP", "127.0.0.1")
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"

    failed = False
    for label, run in (
        ("QBITTORRENT_API_KEY", lambda key: qbittorrent(host, key)),
        ("SONARR_API_KEY", lambda key: arr("sonarr", 8989, host, key)),
        ("RADARR_API_KEY", lambda key: arr("radarr", 7878, host, key)),
    ):
        key = env.get(label)
        if not key:
            print(f"skipped: {label} is empty in .env")
            failed = True
            continue
        try:
            run(key)
        except Exception as error:  # noqa: BLE001 - report and continue with the next service
            print(f"  ERROR: {error}")
            failed = True

    print(f"\n{changes} change(s) {'needed' if CHECK else 'applied'}.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
