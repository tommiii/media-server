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
  4. Prowlarr: login, FlareSolverr proxy + tag, links to Sonarr and Radarr.
Indexers are not touched.
"""
import copy
import http.cookiejar
import json
import re
import subprocess
import sys
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
PORTS = {"sonarr": 8989, "radarr": 7878, "prowlarr": 9696, "qbittorrent": 8080}
API = {"sonarr": "/api/v3", "radarr": "/api/v3", "prowlarr": "/api/v1"}
OPAQUE = ("password", "apiKey")  # the API returns these masked, so they cannot be compared
changes = 0
problems = 0


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

    def login(u, p):
        answer = request("POST", f"{base}/api/v2/auth/login", referer, form={"username": u, "password": p}, opener=opener)
        return isinstance(answer, str) and answer.strip() == "Ok."

    logged_in = bool(password) and login(user, password)
    first_run = False
    if not logged_in:
        temp = temporary_password()  # at most one more attempt: qBittorrent bans after 5 failures
        if temp and login("admin", temp):
            logged_in, first_run = True, True
    if not logged_in:
        problem("cannot sign in. Set QBITTORRENT_USERNAME/QBITTORRENT_PASSWORD in .env to the web UI "
                "credentials (or restart the container to get a new temporary password) and run again")
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


def prowlarr(host, key, cfg, auth, keys):
    print("Prowlarr")
    base = f"http://{host}:{PORTS['prowlarr']}{API['prowlarr']}"
    headers = {"X-Api-Key": key}
    apply_auth(base, headers, auth)

    proxy = cfg.get("flaresolverr")
    if proxy:
        label = proxy.get("tag", "flaresolverr").lower()
        tags = request("GET", f"{base}/tag", headers)
        tag = next((t for t in tags if t["label"].lower() == label), None)
        if tag is None:
            note(f"tag '{label}'")
            tag = {"id": -1} if CHECK else request("POST", f"{base}/tag", headers, body={"label": label})
        upsert_provider(base, headers, "indexerproxy", "FlareSolverr", "FlareSolverr", {"host": proxy["host"]}, tags=[tag["id"]])

    for app, settings in (cfg.get("apps") or {}).items():
        if not keys.get(app):
            print(f"  {app}: skipped (no API key)")
            continue
        wanted = {"prowlarrUrl": settings["prowlarr_url"], "baseUrl": settings["url"], "apiKey": keys[app]}
        upsert_provider(base, headers, "applications", app.capitalize(), app.capitalize(), wanted, top={"syncLevel": "fullSync"})


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

    print("API keys")
    keys = read_api_keys(env)
    for app, value in keys.items():
        if value:
            remember_key(env, f"{app.upper()}_API_KEY", value)
        else:
            problem(f"no API key for {app}: has it started once? (looked in services/{app}/config.xml and .env)")

    qbit = None
    try:
        qbit = qbittorrent(host, cfg["qbittorrent"], env)
    except ApiError as error:
        problem(f"qBittorrent: {error}")

    for app in ("sonarr", "radarr"):
        if keys.get(app):
            try:
                arr(app, host, keys[app], cfg[app], auth, qbit)
            except ApiError as error:
                problem(f"{app}: {error}")
    if keys.get("prowlarr"):
        try:
            prowlarr(host, keys["prowlarr"], cfg["prowlarr"], auth, keys)
        except ApiError as error:
            problem(f"prowlarr: {error}")

    print(f"\n{changes} change(s) {'needed' if CHECK else 'applied'}, {problems} problem(s).")
    if not CHECK and changes:
        print("If Sonarr/Radarr/Prowlarr keep asking for no login, restart them: docker compose restart sonarr radarr prowlarr")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
