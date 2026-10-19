#!/usr/bin/env python3
"""Remove what Sonarr grabbed before the episode's release day: those releases are fakes (an .exe, a password archive).

    ./scripts/guard_unaired.py            report what it would remove, change nothing
    ./scripts/guard_unaired.py --apply    remove them (this is what the cron job runs, every minute)

Sonarr 4.0.20 has no "Reject Unaired Releases" (it comes with Sonarr 5), so RSS happily grabs an "S06E06" two days
before S06E06 exists. This script does the same job after the grab: every download in Sonarr's queue that brings an
episode whose release day (in TIMEZONE of .env) has not come yet is removed from the queue and from qBittorrent, and
blocklisted, so that release is never grabbed again. No new search is started: on the release day RSS and the usual
searches find the real one.

Settings, from `sonarr.release_profile` in config/arr.yml:
  reject_unaired: false   switches it off
  grace_days: N           a release is allowed from N days after the release day (negative: that many days before)
Episodes without an air date are left alone. Prints only when it finds something, so the cron log stays short.
"""
import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required:  sudo apt install python3-yaml   (or: pip install pyyaml)")

ROOT = Path(__file__).resolve().parent.parent


def load_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        quoted = re.match(r"""^(['"])(.*?)\1(?:\s+#.*)?$""", value)
        env[key.strip()] = quoted.group(2) if quoted else value.split(" #")[0].strip()
    return env


def call(base, key, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Api-Key": key, **({"Content-Type": "application/json"} if data else {})}
    with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method=method, headers=headers), timeout=60) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    if not env.get("SONARR_API_KEY"):
        sys.exit("missing in .env: SONARR_API_KEY (run ./scripts/apply_arr_config.py)")
    cfg = ((yaml.safe_load((ROOT / "config/arr.yml").read_text()) or {}).get("sonarr") or {}).get("release_profile") or {}
    if not cfg.get("reject_unaired", True):
        return
    grace = datetime.timedelta(days=int(cfg.get("grace_days", 0)))
    zone = ZoneInfo(env.get("TIMEZONE") or "UTC")
    today = datetime.datetime.now(zone).date()
    host = env.get("LAN_IP", "127.0.0.1")
    base = f"http://{'127.0.0.1' if host in ('', '0.0.0.0') else host}:8989"
    key = env["SONARR_API_KEY"]

    # one download can be several queue records (a season pack: one per episode)
    downloads = {}
    for r in call(base, key, "GET", "/api/v3/queue?page=1&pageSize=1000&includeEpisode=true&includeSeries=true")["records"]:
        downloads.setdefault(r.get("downloadId") or f"id-{r['id']}", []).append(r)

    for records in downloads.values():
        early = []
        for r in records:
            aired = (r.get("episode") or {}).get("airDateUtc")
            if not aired:
                continue
            day = datetime.datetime.fromisoformat(aired.replace("Z", "+00:00")).astimezone(zone).date()
            if today < day + grace:
                early.append((r["episode"], day))
        if not early:
            continue
        episode, day = min(early, key=lambda e: e[1])
        series = (records[0].get("series") or {}).get("title", "?")
        code = f"S{episode['seasonNumber']:02d}E{episode['episodeNumber']:02d}"
        stamp = datetime.datetime.now(zone).strftime("%Y-%m-%d %H:%M")
        print(f"{stamp} {'EXCLUDED' if args.apply else 'WOULD EXCLUDE'} {records[0].get('title', '?')}"
              f" ({records[0].get('indexer', '?')}): {series} {code} comes out on {day:%a %d %b}, a release before then is a fake")
        if args.apply:
            call(base, key, "DELETE", "/api/v3/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=true",
                 body={"ids": [r["id"] for r in records]})


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as error:
        sys.exit(f"cannot reach Sonarr: {error}")
