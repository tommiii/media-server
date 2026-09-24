#!/usr/bin/env python3
"""Configure qBittorrent, Sonarr, Radarr and Prowlarr from arr.yml. Idempotent.

    ./apply_arr_config.py            apply
    ./apply_arr_config.py --check    show what would change, change nothing
    ./apply_arr_config.py --force    also rewrite passwords/API keys inside existing entries

Run it on the server, from the repository, once the containers are up. It talks to the UIs
published on LAN_IP (see .env). Needs PyYAML: sudo apt install python3-yaml

What it does, in this order
  1. Reads the API keys of Sonarr/Radarr/Prowlarr from their config.xml and, if they are empty
     in .env, writes them there (Homepage and Recyclarr use them).
  2. qBittorrent: signs in (on first start with the temporary password from `docker logs`),
     sets the web UI user/password, applies the preferences, makes sure an API key exists.
  3. Sonarr/Radarr: login, hardlinks, root folders, qBittorrent download client (the app tests the
     connection), removes leftover clients (e.g. Deluge).
  4. Prowlarr: login, FlareSolverr proxy + tag, the indexers listed in arr.yml (the app tests each
     one), links to Sonarr and Radarr.
  5. Sonarr/Radarr: optionally assigns the quality profile Recyclarr created to the existing
     series/movies (assign_to_existing in arr.yml, OFF by default: it can make the apps replace files
     whose quality the profile does not allow).
  6. Plex: reads its token from Preferences.xml, sets the preferences and creates the libraries
     listed in arr.yml. Best effort: Plex's API is not versioned like the *arr ones.
It waits for each service to answer before talking to it.
"""
import copy
import http.cookiejar
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required:  sudo apt install python3-yaml   (or: pip install pyyaml)")

ROOT = Path(__file__).resolve().parent
CHECK = "--check" in sys.argv
FORCE = "--force" in sys.argv
PORTS = {"sonarr": 8989, "radarr": 7878, "prowlarr": 9696, "qbittorrent": 8080, "plex": 32400}
API = {"sonarr": "/api/v3", "radarr": "/api/v3", "prowlarr": "/api/v1"}
OPAQUE = ("password", "apiKey")  # the API returns these masked, so they cannot be compared
changes = 0
problems = 0
PENDING_TAGS = set()


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- helpers
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


def set_env(path, key, value):
    """Fill KEY= in .env (only used when it is empty or missing)."""
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(key)}\s*=", line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def expand(node, env):
    if isinstance(node, dict):
        return {k: expand(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [expand(v, env) for v in node]
    if isinstance(node, str):
        def sub(m):
            value = env.get(m.group(1))
            if value:
                return value
            if m.group(2) is not None:
                return m.group(2)
            raise SystemExit(f"arr.yml uses ${{{m.group(1)}}} but it is not set in .env")
        return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", sub, node)
    return node


def note(message):
    global changes
    changes += 1
    print(("  would change: " if CHECK else "  changed:      ") + message)


def problem(message):
    global problems
    problems += 1
    print(f"  PROBLEM: {message}")


def request(method, url, headers=None, body=None, form=None, opener=None):
    data = None
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with (opener.open(req, timeout=60) if opener else urllib.request.urlopen(req, timeout=60)) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            detail = "; ".join(f"{e.get('propertyName', '')}: {e.get('errorMessage', '')}".strip(": ")
                               for e in json.loads(raw))
        except (ValueError, AttributeError, TypeError):
            detail = raw[:200]
        raise ApiError(f"HTTP {error.code} {detail}".strip()) from None
    except urllib.error.URLError as error:
        raise ApiError(f"cannot reach {url}: {error.reason}") from None
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw


def wait_ready(label, url, timeout=240):
    """Wait until something answers on url (any HTTP status counts: 401/302 mean the app is up)."""
    deadline = time.time() + timeout
    waited = False
    while True:
        try:
            urllib.request.urlopen(url, timeout=5)
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            if time.time() > deadline:
                problem(f"{label} does not answer on {url} after {timeout}s: is the container up? (docker compose ps)")
                return False
            if not waited:
                print(f"  waiting for {label}...")
                waited = True
            time.sleep(3)


def enum_value(current, name, code):
    """Send an enum the same way the API returned it (string or number)."""
    return code if isinstance(current, int) else name


# ---------------------------------------------------------------- API keys
def read_api_keys(env):
    keys = {}
    base = env.get("BASE_DIR", "")
    for app in ("sonarr", "radarr", "prowlarr"):
        value = env.get(f"{app.upper()}_API_KEY", "")
        cfg = Path(base) / "services" / app / "config.xml"
        if not value and cfg.exists():
            match = re.search(r"<ApiKey>([^<]+)</ApiKey>", cfg.read_text())
            value = match.group(1) if match else ""
        keys[app] = value
    return keys


def remember_key(env, name, value):
    if value and not env.get(name):
        env[name] = value
        if CHECK:
            print(f"  would write {name} to .env")
        else:
            set_env(ROOT / ".env", name, value)
            print(f"  wrote {name} to .env")


# ---------------------------------------------------------------- qBittorrent
def temporary_password():
    try:
        logs = subprocess.run(["docker", "logs", "qbittorrent"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.findall(r"temporary password is provided for this session:\s*(\S+)", logs.stdout + logs.stderr)
    return found[-1] if found else None


def qbittorrent(host, cfg, env):
    print("qBittorrent")
    base = f"http://{host}:{PORTS['qbittorrent']}"
    referer = {"Referer": base, "Origin": base}
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    user, password = cfg["username"], cfg.get("password") or ""

    reasons = []

    def login(u, p):
        # qBittorrent >= 5.2 answers 204 (empty) on success and 401 on failure; older versions 200 "Ok." / "Fails."
        try:
            answer = request("POST", f"{base}/api/v2/auth/login", referer, form={"username": u, "password": p}, opener=opener)
        except ApiError as error:
            reasons.append(str(error))
            return False
        if answer is None or (isinstance(answer, str) and answer.strip() == "Ok."):
            return True
        reasons.append(f"answered {answer!r}")
        return False

    logged_in = bool(password) and login(user, password)
    first_run = False
    if not logged_in:
        temp = temporary_password()  # at most one more attempt: qBittorrent bans after 5 failures
        if temp and login("admin", temp):
            logged_in, first_run = True, True
    if not logged_in:
        why = reasons[0] if reasons else "no password set in .env and no temporary password in the container log"
        hint = ("this address is banned after too many failed logins: docker compose restart qbittorrent"
                if any("403" in r for r in reasons) else
                "set QBITTORRENT_USERNAME/QBITTORRENT_PASSWORD in .env to the credentials qBittorrent really has "
                "(or reset its password: README, troubleshooting) and run again")
        problem(f"cannot sign in to qBittorrent as '{user}' ({why}). {hint}")
        return None

    wanted = dict(cfg.get("preferences") or {})
    current = request("GET", f"{base}/api/v2/app/preferences", referer, opener=opener)
    diff = {k: v for k, v in wanted.items() if current.get(k) != v}
    if first_run and password:
        diff["web_ui_username"], diff["web_ui_password"] = user, password
        note(f"web UI login set to user '{user}'")
    elif first_run:
        problem("signed in with the temporary password: set QBITTORRENT_PASSWORD in .env and run again "
                "to replace it with a permanent one")
        wanted = {}
    shown = {k: v for k, v in diff.items() if k != "web_ui_password"}
    if shown:
        note("preferences: " + ", ".join(f"{k}={v}" for k, v in shown.items() if k != "web_ui_username"))
    if diff and not CHECK:
        request("POST", f"{base}/api/v2/app/setPreferences", referer, form={"json": json.dumps(diff)}, opener=opener)
        if "web_ui_password" in diff:  # session survives a password change, but log in again to be safe
            login(user, password)
    if not diff:
        print("  ok")

    key = current.get("web_ui_api_key") or ""
    if not key and not CHECK:
        answer = request("POST", f"{base}/api/v2/app/rotateAPIKey", referer, opener=opener)
        key = (answer or {}).get("apiKey", "") if isinstance(answer, dict) else ""
        note("generated an API key")
    remember_key(env, "QBITTORRENT_API_KEY", key)
    if first_run and not password:
        return None  # nothing usable to give Sonarr/Radarr yet
    return {"user": user, "password": password}


# ---------------------------------------------------------------- Sonarr / Radarr / Prowlarr
def camel(name):
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def upsert_provider(base, headers, kind, implementation, name, wanted, top=None, tags=None):
    """Create or update a provider (download client, application, proxy) from the app's own schema."""
    existing = next((p for p in request("GET", f"{base}/{kind}", headers) if p["implementation"] == implementation), None)
    if existing:
        body = copy.deepcopy(existing)
    else:
        schema = request("GET", f"{base}/{kind}/schema", headers)
        template = next((s for s in schema if s["implementation"] == implementation), None)
        if template is None:
            problem(f"{implementation} is not available in this version")
            return
        body = copy.deepcopy(template)
        body["name"] = name
        if "enable" in body:
            body["enable"] = True

    field_names = {f["name"] for f in body["fields"]}
    wanted = dict(wanted)
    if "category" in wanted:
        category = wanted.pop("category")
        wanted["tvCategory" if "tvCategory" in field_names else "movieCategory"] = category

    diffs = []
    for field in body["fields"]:
        if field["name"] not in wanted:
            continue
        if existing and field.get("privacy") in OPAQUE and not FORCE:
            continue
        if field.get("value") != wanted[field["name"]]:
            diffs.append(field["name"])
            field["value"] = wanted[field["name"]]
    for key, value in (top or {}).items():
        if body.get(key) != value:
            diffs.append(key)
            body[key] = value
    if tags is not None and set(body.get("tags") or []) != set(tags):
        diffs.append("tags")
        body["tags"] = sorted(tags)

    if existing and not diffs:
        print(f"  {implementation}: ok")
        return
    note(f"{implementation} {'updated: ' + ', '.join(diffs) if existing else 'created'}")
    if CHECK:
        return
    try:
        if existing:
            request("PUT", f"{base}/{kind}/{body['id']}?forceSave=false", headers, body=body)
        else:
            request("POST", f"{base}/{kind}?forceSave=false", headers, body=body)
    except ApiError as error:
        problem(f"{implementation}: {error}")


def apply_auth(base, headers, auth):
    if not auth.get("username") or not auth.get("password"):
        print("  login: skipped (ARR_USERNAME / ARR_PASSWORD empty in .env)")
        return
    host = request("GET", f"{base}/config/host", headers)
    method, required = str(host.get("authenticationMethod")).lower(), str(host.get("authenticationRequired")).lower()
    if method in ("forms", "2") and required in ("enabled", "0") and host.get("username") == auth["username"] and not FORCE:
        print("  login: ok")
        return
    note(f"login: Forms authentication for user '{auth['username']}'")
    if CHECK:
        return
    host["authenticationMethod"] = enum_value(host.get("authenticationMethod"), "forms", 2)
    host["authenticationRequired"] = enum_value(host.get("authenticationRequired"), "enabled", 0)
    host["username"], host["password"], host["passwordConfirmation"] = auth["username"], auth["password"], auth["password"]
    try:
        request("PUT", f"{base}/config/host/{host['id']}", headers, body=host)
    except ApiError as error:
        problem(f"login: {error}")


def arr(name, host, key, cfg, auth, qbit):
    print(name.capitalize())
    base = f"http://{host}:{PORTS[name]}{API[name]}"
    headers = {"X-Api-Key": key}
    apply_auth(base, headers, auth)

    media = request("GET", f"{base}/config/mediamanagement", headers)
    if "hardlinks" in cfg and media.get("copyUsingHardlinks") != cfg["hardlinks"]:
        note(f"hardlinks -> {cfg['hardlinks']}")
        if not CHECK:
            media["copyUsingHardlinks"] = cfg["hardlinks"]
            request("PUT", f"{base}/config/mediamanagement/{media['id']}", headers, body=media)

    have = {r["path"].rstrip("/") for r in request("GET", f"{base}/rootfolder", headers)}
    for path in cfg.get("root_folders", []):
        if path.rstrip("/") in have:
            print(f"  root folder {path}: ok")
            continue
        note(f"root folder {path}")
        if not CHECK:
            try:
                request("POST", f"{base}/rootfolder", headers, body={"path": path})
            except ApiError as error:
                problem(f"root folder {path}: {error} (does the folder exist and belong to PUID:PGID?)")

    dc = dict(cfg.get("download_client") or {})
    if dc and qbit:
        name_ = dc.pop("name", "qBittorrent")
        wanted = {camel(k): v for k, v in dc.items()}
        wanted.update({"username": qbit["user"], "password": qbit["password"]})
        upsert_provider(base, headers, "downloadclient", "QBittorrent", name_, wanted)
    elif dc:
        print("  download client: skipped (qBittorrent login unavailable)")

    for implementation in cfg.get("remove_download_clients", []):
        for client in request("GET", f"{base}/downloadclient", headers):
            if client["implementation"].lower() == implementation.lower():
                note(f"remove download client '{client['name']}'")
                if not CHECK:
                    request("DELETE", f"{base}/downloadclient/{client['id']}", headers)

    profile = cfg.get("quality_profile")
    if profile and profile.get("assign_to_existing"):
        assign_profile(name, base, headers, profile["name"])


def assign_profile(name, base, headers, profile_name):
    profiles = request("GET", f"{base}/qualityprofile", headers)
    profile = next((p for p in profiles if p["name"].lower() == profile_name.lower()), None)
    if profile is None:
        print(f"  quality profile '{profile_name}': not found yet (run `recyclarr sync` first, then this again)")
        return
    sonarr = name == "sonarr"
    items = request("GET", f"{base}/{'series' if sonarr else 'movie'}", headers)
    ids = [i["id"] for i in items if i.get("qualityProfileId") != profile["id"]]
    if not ids:
        print(f"  quality profile '{profile_name}': all {len(items)} titles already use it")
        return
    note(f"{len(ids)} of {len(items)} titles -> quality profile '{profile_name}'")
    if not CHECK:
        body = {"seriesIds" if sonarr else "movieIds": ids, "qualityProfileId": profile["id"]}
        request("PUT", f"{base}/{'series' if sonarr else 'movie'}/editor", headers, body=body)


def ensure_tag(base, headers, label):
    label = label.lower()
    tag = next((t for t in request("GET", f"{base}/tag", headers) if t["label"].lower() == label), None)
    if tag is None:
        if CHECK:
            if label not in PENDING_TAGS:
                PENDING_TAGS.add(label)
                note(f"tag '{label}'")
            return -1
        note(f"tag '{label}'")
        return request("POST", f"{base}/tag", headers, body={"label": label})["id"]
    return tag["id"]


def prowlarr_indexers(base, headers, items):
    if not items:
        return
    existing = {i.get("definitionName"): i for i in request("GET", f"{base}/indexer", headers)}
    schema = None
    profile_id = None
    for item in items:
        spec = {"definition": item} if isinstance(item, str) else dict(item)
        definition = spec["definition"]
        tag_ids = [ensure_tag(base, headers, t) for t in spec.get("tags", [])]
        current = existing.get(definition)
        if current is not None:
            if tag_ids and set(current.get("tags") or []) != set(tag_ids):
                note(f"indexer {definition}: tags")
                if not CHECK:
                    current["tags"] = sorted(tag_ids)
                    try:
                        request("PUT", f"{base}/indexer/{current['id']}?forceSave=true", headers, body=current)
                    except ApiError as error:
                        problem(f"indexer {definition}: {error}")
            else:
                print(f"  indexer {definition}: ok")
            continue
        if schema is None:
            schema = request("GET", f"{base}/indexer/schema", headers)
            profile_id = request("GET", f"{base}/appprofile", headers)[0]["id"]
        template = next((t for t in schema if t.get("definitionName") == definition), None)
        if template is None:
            problem(f"indexer '{definition}' does not exist in this Prowlarr version (name as in its indexer list)")
            continue
        body = copy.deepcopy(template)
        body.update(name=spec.get("name", template["name"]), enable=True, appProfileId=profile_id,
                    priority=spec.get("priority", template.get("priority", 25)), tags=sorted(tag_ids))
        for field in body["fields"]:
            if field["name"] in spec.get("fields", {}):
                field["value"] = spec["fields"][field["name"]]
        note(f"indexer {definition} added")
        if not CHECK:
            try:
                request("POST", f"{base}/indexer?forceSave=false", headers, body=body)
            except ApiError as error:
                problem(f"indexer {definition}: {error}")


def prowlarr(host, key, cfg, auth, keys):
    print("Prowlarr")
    base = f"http://{host}:{PORTS['prowlarr']}{API['prowlarr']}"
    headers = {"X-Api-Key": key}
    apply_auth(base, headers, auth)

    proxy = cfg.get("flaresolverr")
    if proxy:
        tag_id = ensure_tag(base, headers, proxy.get("tag", "flaresolverr"))
        upsert_provider(base, headers, "indexerproxy", "FlareSolverr", "FlareSolverr", {"host": proxy["host"]}, tags=[tag_id])

    prowlarr_indexers(base, headers, cfg.get("indexers"))

    for app, settings in (cfg.get("apps") or {}).items():
        if not keys.get(app):
            print(f"  {app}: skipped (no API key)")
            continue
        wanted = {"prowlarrUrl": settings["prowlarr_url"], "baseUrl": settings["url"], "apiKey": keys[app]}
        upsert_provider(base, headers, "applications", app.capitalize(), app.capitalize(), wanted, top={"syncLevel": "fullSync"})


# ---------------------------------------------------------------- Plex
def plex_token(env):
    if env.get("PLEX_TOKEN"):
        return env["PLEX_TOKEN"]
    prefs = Path(env.get("BASE_DIR", "")) / "services" / "plex" / "Library" / "Application Support" / "Plex Media Server" / "Preferences.xml"
    if prefs.exists():
        match = re.search(r'PlexOnlineToken="([^"]+)"', prefs.read_text())
        return match.group(1) if match else ""
    return ""


def plex_create_library(base, headers, name, kind, path):
    """Plex has changed this endpoint over time: try the current agents, the legacy ones, then the newer route."""
    movie = kind == "movie"
    variants = [
        ("/library/sections", {"name": name, "type": kind, "location": path, "language": "en-US",
                               "agent": "tv.plex.agents.movie" if movie else "tv.plex.agents.series",
                               "scanner": "Plex Movie" if movie else "Plex TV Series"}),
        ("/library/sections", {"name": name, "type": kind, "location": path, "language": "en-US",
                               "agent": "com.plexapp.agents.imdb" if movie else "com.plexapp.agents.thetvdb",
                               "scanner": "Plex Movie Scanner" if movie else "Plex Series Scanner"}),
        ("/library/sections/all", {"name": name, "type": 1 if movie else 2, "locations": path, "language": "en-US",
                                   "agent": "tv.plex.agents.movie" if movie else "tv.plex.agents.series",
                                   "scanner": "Plex Movie" if movie else "Plex TV Series"}),
    ]
    last = "unknown error"
    for route, params in variants:
        try:
            request("POST", f"{base}{route}?{urllib.parse.urlencode(params)}", headers)
        except ApiError as error:
            last = str(error)
            continue
        sections = request("GET", f"{base}/library/sections", headers)["MediaContainer"].get("Directory", [])
        if any(d["title"] == name for d in sections):
            return True
    problem(f"library '{name}': {last}")
    return False


def plex(host, cfg, env):
    print("Plex")
    base = f"http://{host}:{PORTS['plex']}"
    if not wait_ready("Plex", f"{base}/identity"):
        return
    identity = request("GET", f"{base}/identity", {"Accept": "application/json"})["MediaContainer"]
    if not identity.get("claimed"):
        problem("the server is not linked to a Plex account. Put a fresh PLEX_CLAIM (https://www.plex.tv/claim/, valid "
                "4 minutes) in .env, run `docker compose up -d --force-recreate plex`, then run this again")
        return
    token = plex_token(env)
    if not token:
        problem("no Plex token found in .env or Preferences.xml")
        return
    remember_key(env, "PLEX_TOKEN", token)
    headers = {"X-Plex-Token": token, "Accept": "application/json"}

    wanted = cfg.get("preferences") or {}
    settings = {x["id"]: x.get("value") for x in request("GET", f"{base}/:/prefs", headers)["MediaContainer"].get("Setting", [])}
    norm = lambda v: str(int(v)) if isinstance(v, bool) else str(v)
    diff = {}
    for key, value in wanted.items():
        if key not in settings:
            print(f"  preference {key}: skipped (this Plex has no such setting)")
        elif norm(settings[key]) != norm(value):
            diff[key] = norm(value)
    if diff:
        note("preferences: " + ", ".join(f"{k}={v}" for k, v in diff.items()))
        if not CHECK:
            try:
                request("PUT", f"{base}/:/prefs?{urllib.parse.urlencode(diff)}", headers)
            except ApiError as error:
                problem(f"preferences: {error}")
    else:
        print("  preferences: ok")

    have = request("GET", f"{base}/library/sections", headers)["MediaContainer"].get("Directory", [])
    for lib in cfg.get("libraries", []):
        if any(d["title"] == lib["name"] for d in have):
            print(f"  library {lib['name']}: ok")
            continue
        note(f"library {lib['name']} ({lib['path']})")
        if not CHECK:
            plex_create_library(base, headers, lib["name"], lib["type"], lib["path"])


# ---------------------------------------------------------------- main
def main():
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit(".env not found")
    env = load_env(env_file)
    loaded = yaml.safe_load((ROOT / "arr.yml").read_text())
    if not isinstance(loaded, dict) or not all(k in loaded for k in ("qbittorrent", "sonarr", "radarr", "prowlarr")):
        sys.exit("arr.yml is empty or incomplete: it needs the sections qbittorrent, sonarr, radarr and prowlarr")
    cfg = expand(loaded, env)
    host = env.get("LAN_IP", "127.0.0.1")
    host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    auth = cfg.get("auth") or {}

    ready = {}
    for label, port in (("qBittorrent", PORTS["qbittorrent"]), ("Sonarr", PORTS["sonarr"]),
                        ("Radarr", PORTS["radarr"]), ("Prowlarr", PORTS["prowlarr"])):
        ready[label.lower()] = wait_ready(label, f"http://{host}:{port}/")

    print("API keys")
    keys = read_api_keys(env)
    for _ in range(20):  # a freshly started app writes config.xml a moment after it answers
        if all(keys.get(a) for a in ("sonarr", "radarr", "prowlarr") if ready[a]):
            break
        time.sleep(3)
        keys = read_api_keys(env)
    for app, value in keys.items():
        if value:
            remember_key(env, f"{app.upper()}_API_KEY", value)
        else:
            problem(f"no API key for {app}: has it started once? (looked in services/{app}/config.xml and .env)")

    qbit = None
    if ready["qbittorrent"]:
        try:
            qbit = qbittorrent(host, cfg["qbittorrent"], env)
        except ApiError as error:
            problem(f"qBittorrent: {error}")

    for app in ("sonarr", "radarr"):
        if keys.get(app) and ready[app]:
            try:
                arr(app, host, keys[app], cfg[app], auth, qbit)
            except ApiError as error:
                problem(f"{app}: {error}")
    if keys.get("prowlarr") and ready["prowlarr"]:
        try:
            prowlarr(host, keys["prowlarr"], cfg["prowlarr"], auth, keys)
        except ApiError as error:
            problem(f"prowlarr: {error}")
    if cfg.get("plex"):
        try:
            plex(host, cfg["plex"], env)
        except ApiError as error:
            problem(f"plex: {error}")

    print(f"\n{changes} change(s) {'needed' if CHECK else 'applied'}, {problems} problem(s).")
    if not CHECK and changes:
        print("If Sonarr/Radarr/Prowlarr keep asking for no login, restart them: docker compose restart sonarr radarr prowlarr")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
