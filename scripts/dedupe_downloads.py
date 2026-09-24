#!/usr/bin/env python3
"""Remove duplicate downloads: unfinished torrents that would give an episode or a movie you are already getting.

    ./scripts/dedupe_downloads.py            report what it would remove, change nothing
    ./scripts/dedupe_downloads.py --apply    remove the duplicates (this is what the daily cron job runs)
    --min-progress 0.9                       never remove a duplicate that is already this far along (default 0.9)
    --keep-manual                            never touch torrents you added by hand to qBittorrent

How "the same" is decided
  Every unfinished torrent is turned into the set of episodes (or the movie) it will deliver:
  - torrents grabbed by Sonarr/Radarr: from their queue
  - torrents added by hand: Sonarr/Radarr's own title parser (/parse) recognises the series/episodes or the movie
  Downloads are then ranked: nearly finished ones first (upgrades are off, the first to finish is imported),
  then quality rank in the title's quality profile (the ladder), then more episodes, then more progress. Going down that list, a download is kept if it brings at least one episode that the
  better ones do not already bring; otherwise it is a duplicate. So a season pack is never removed because of
  a single episode, and nothing is removed unless something better delivers every episode it would.

How it is removed
  Sonarr/Radarr downloads: through their queue (removeFromClient, no blocklist, no new search).
  Manual torrents: deleted from qBittorrent together with their files.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTS = {"sonarr": 8989, "radarr": 7878, "qbittorrent": 8080}


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


class Api:
    def __init__(self, host, port, headers):
        self.base, self.headers = f"http://{host}:{port}", headers

    def call(self, method, path, body=None, form=None):
        data, headers = None, dict(self.headers)
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
        return json.loads(raw) if raw.strip() else None


def rank_maps(profiles):
    """profile id -> {quality id: position}; a higher position is a better quality (the API lists worst first)."""
    out = {}
    for profile in profiles:
        positions = {}
        for index, item in enumerate(profile["items"]):
            if item.get("quality"):
                positions[item["quality"]["id"]] = index
            for sub in item.get("items") or []:
                positions[sub["quality"]["id"]] = index
        out[profile["id"]] = positions
    return out


def quality_id(quality_model):
    return ((quality_model or {}).get("quality") or {}).get("id")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-progress", type=float, default=0.9)
    parser.add_argument("--keep-manual", action="store_true")
    args = parser.parse_args()

    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit(".env not found")
    env = load_env(env_file)
    host = env.get("LAN_IP", "127.0.0.1")
    host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    missing = [k for k in ("SONARR_API_KEY", "RADARR_API_KEY", "QBITTORRENT_API_KEY") if not env.get(k)]
    if missing:
        sys.exit(f"missing in .env: {', '.join(missing)} (run ./scripts/apply_arr_config.py)")

    qbit = Api(host, PORTS["qbittorrent"], {"Authorization": f"Bearer {env['QBITTORRENT_API_KEY']}"})
    sonarr = Api(host, PORTS["sonarr"], {"X-Api-Key": env["SONARR_API_KEY"]})
    radarr = Api(host, PORTS["radarr"], {"X-Api-Key": env["RADARR_API_KEY"]})

    torrents = {t["hash"].lower(): t for t in qbit.call("GET", "/api/v2/torrents/info") if t["progress"] < 1}
    if not torrents:
        print("No unfinished torrents.")
        return

    ranks = {"tv": rank_maps(sonarr.call("GET", "/api/v3/qualityprofile")),
             "movie": rank_maps(radarr.call("GET", "/api/v3/qualityprofile"))}
    series_profile = {s["id"]: s["qualityProfileId"] for s in sonarr.call("GET", "/api/v3/series")}
    movie_profile = {m["id"]: m["qualityProfileId"] for m in radarr.call("GET", "/api/v3/movie")}

    items = {}  # torrent hash -> download

    def entry(h, kind):
        t = torrents[h]
        return items.setdefault(h, {"hash": h, "kind": kind, "name": t["name"], "progress": t["progress"], "keys": set(),
                                    "rank": -1, "queue": {"tv": [], "movie": []}, "manual": False})

    for r in sonarr.call("GET", "/api/v3/queue?page=1&pageSize=1000&includeUnknownSeriesItems=false")["records"]:
        h = (r.get("downloadId") or "").lower()
        if h in torrents and r.get("episodeId"):
            it = entry(h, "tv")
            it["keys"].add(("tv", r["episodeId"]))
            it["queue"]["tv"].append(r["id"])
            it["rank"] = ranks["tv"].get(series_profile.get(r.get("seriesId")), {}).get(quality_id(r.get("quality")), -1)
    for r in radarr.call("GET", "/api/v3/queue?page=1&pageSize=1000")["records"]:
        h = (r.get("downloadId") or "").lower()
        if h in torrents and r.get("movieId"):
            it = entry(h, "movie")
            it["keys"].add(("movie", r["movieId"]))
            it["queue"]["movie"].append(r["id"])
            it["rank"] = ranks["movie"].get(movie_profile.get(r.get("movieId")), {}).get(quality_id(r.get("quality")), -1)

    if not args.keep_manual:
        for h in torrents:
            if h in items:
                continue
            title = urllib.parse.quote(torrents[h]["name"])
            parsed = sonarr.call("GET", f"/api/v3/parse?title={title}") or {}
            if parsed.get("series") and parsed.get("episodes"):
                it = entry(h, "tv")
                it["manual"] = True
                it["keys"] = {("tv", e["id"]) for e in parsed["episodes"]}
                q = quality_id((parsed.get("parsedEpisodeInfo") or {}).get("quality"))
                it["rank"] = ranks["tv"].get(parsed["series"].get("qualityProfileId"), {}).get(q, -1)
                continue
            parsed = radarr.call("GET", f"/api/v3/parse?title={title}") or {}
            if parsed.get("movie") and parsed["movie"].get("id"):
                it = entry(h, "movie")
                it["manual"] = True
                it["keys"] = {("movie", parsed["movie"]["id"])}
                q = quality_id((parsed.get("parsedMovieInfo") or {}).get("quality"))
                it["rank"] = ranks["movie"].get(parsed["movie"].get("qualityProfileId"), {}).get(q, -1)

    # A download that is nearly done wins over a better-ranked one that has barely started: upgrades are off, so
    # the first to finish is the one that gets imported. Then the ladder, then more episodes, then progress.
    ordered = sorted(items.values(), key=lambda i: (i["progress"] < args.min_progress, -i["rank"], -len(i["keys"]),
                                                    -i["progress"], i["manual"]))
    covered, duplicates = set(), []
    for it in ordered:
        if it["keys"] and it["keys"] <= covered:
            duplicates.append(it)
        else:
            covered |= it["keys"]

    print(f"{len(torrents)} unfinished torrent(s), {len(items)} recognised, {len(duplicates)} duplicate(s).")
    if not duplicates:
        return
    removed = 0
    for it in duplicates:
        origin = "added by hand" if it["manual"] else f"grabbed by {'Sonarr' if it['kind'] == 'tv' else 'Radarr'}"
        better = next((o for o in ordered[:ordered.index(it)] if o["keys"] & it["keys"]), None)
        line = f"  {it['name'][:80]}  ({origin}, {it['progress'] * 100:.0f} %)\n      duplicate of: {(better['name'] if better else '?')[:80]}"
        if it["progress"] >= args.min_progress:  # two nearly finished copies: both stay, the extra one is cleaned up later
            print(f"KEEP  {line}\n      (already {it['progress'] * 100:.0f} % done: let it finish)")
            continue
        print(("REMOVE" if args.apply else "WOULD REMOVE") + line)
        if not args.apply:
            continue
        if it["manual"]:
            qbit.call("POST", "/api/v2/torrents/delete", form={"hashes": it["hash"], "deleteFiles": "true"})
        else:
            app, key = (sonarr, "tv") if it["kind"] == "tv" else (radarr, "movie")
            app.call("DELETE", "/api/v3/queue/bulk?removeFromClient=true&blocklist=false&skipRedownload=true", body={"ids": it["queue"][key]})
        removed += 1
    if args.apply:
        print(f"Removed {removed} duplicate download(s).")
    else:
        print("Nothing was changed (run with --apply to remove).")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as error:
        sys.exit(f"cannot reach the apps: {error}")
