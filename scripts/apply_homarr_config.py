#!/usr/bin/env python3
"""Configure Homarr from the `homarr:` section of arr.yml. Idempotent and additive.

    ./scripts/apply_homarr_config.py            apply
    ./scripts/apply_homarr_config.py --check    show what would change, change nothing

Run it on the server, from the repository, once the containers are up. It talks to Homarr on LAN_IP:80 (see .env) with
HOMARR_API_KEY. Homarr keeps its settings in its own database and has no config file, so this is how the integrations and
the default board stay in this repository. Needs PyYAML: sudo apt install python3-yaml

What it does, in this order
  1. Integrations (Plex, Sonarr, Radarr, Prowlarr, qBittorrent): created if no integration of that name exists. Homarr tests
     the connection before saving it, and creates the launcher tile ("app") that goes with it.
  2. The default board: created only if no board of that name exists, then filled with the tiles of arr.yml.
Nothing that already exists is changed or deleted.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required:  sudo apt install python3-yaml   (or: pip install pyyaml)")

ROOT = Path(__file__).resolve().parent.parent  # the repository root (this file lives in scripts/)
CHECK = "--check" in sys.argv
ICONS = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/{}.svg"
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
    print(("  would create: " if CHECK else "  created:      ") + message)


def problem(message):
    global problems
    problems += 1
    print(f"  PROBLEM: {message}")


def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(headers)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            parsed = json.loads(raw)
            detail = parsed.get("message") or (parsed.get("error") or {}).get("json", {}).get("message") or raw[:200]
        except (ValueError, AttributeError, TypeError):
            detail = raw[:200]
        raise ApiError(f"HTTP {error.code} {detail}".strip()) from None
    except (urllib.error.URLError, OSError) as error:
        raise ApiError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from None
    return json.loads(raw) if raw else None


def trpc(base, headers, procedure, body=None):
    """tRPC over HTTP with the SuperJSON transformer: input and output are wrapped in {"json": ...}."""
    if body is None:
        result = http("GET", f"{base}/api/trpc/{procedure}", headers)
    else:
        result = http("POST", f"{base}/api/trpc/{procedure}", headers, {"json": body})
    return result["result"]["data"]["json"]


def wait_ready(base, timeout=240):
    deadline = time.time() + timeout
    waited = False
    while True:
        try:
            urllib.request.urlopen(base, timeout=5)
            return True
        except urllib.error.HTTPError:
            return True  # it answered
        except (urllib.error.URLError, OSError):
            if time.time() > deadline:
                problem(f"Homarr does not answer on {base} after {timeout}s: is the container up? "
                        "(docker compose ps homarr; docker compose logs homarr)")
                return False
            if not waited:
                print("  waiting for Homarr...")
                waited = True
            time.sleep(3)


# ---------------------------------------------------------------- steps
def integrations(base, headers, wanted):
    """Returns {name: {"id": integration id, "appId": launcher tile id or None}} for every integration of arr.yml."""
    existing = {i["name"]: i for i in trpc(base, headers, "integration.all")}
    result = {}
    for item in wanted:
        name = item["name"]
        if name in existing:
            result[name] = {"id": existing[name]["id"], "appId": None}
            continue
        note(f"integration {name}")
        if CHECK:
            continue
        secrets = [{"kind": kind, "value": str(value)} for kind, value in item["secrets"].items() if value]
        if not secrets:
            problem(f"{name}: its credentials are empty in .env (run ./scripts/apply_arr_config.py first)")
            continue
        body = {"name": name, "url": item["url"], "kind": item["kind"], "secrets": secrets,
                "attemptSearchEngineCreation": False}
        if item.get("app"):
            app = item["app"]
            body["app"] = {"name": name, "description": app.get("description"), "iconUrl": ICONS.format(app["icon"]),
                           "href": app.get("href"), "pingUrl": item["url"]}
        try:
            created = trpc(base, headers, "integration.create", body)
        except ApiError as error:
            problem(f"{name}: {error}")
            continue
        if created.get("error"):  # Homarr tested the connection and it failed
            problem(f"{name}: {created['error']}")
            continue
        result[name] = {"id": created["integration"]["id"], "appId": created.get("appId")}
    return result


def board(base, headers, cfg, ids):
    name = cfg["name"]
    if any(b["name"] == name for b in http("GET", f"{base}/api/boards", headers)):
        print(f"  board {name} exists: left alone")
        return
    note(f"board {name} with {len(cfg['tiles'])} tiles")
    if CHECK:
        return
    apps = {a["name"]: a["id"] for a in trpc(base, headers, "app.all")}
    created = http("POST", f"{base}/api/boards", headers,
                   {"name": name, "columnCount": cfg.get("columns", 12), "isPublic": False})
    for tile in cfg["tiles"]:
        options = dict(tile.get("options") or {})
        if tile["kind"] == "app":
            if tile["app"] not in apps:
                problem(f"tile {tile['app']}: no such app in Homarr")
                continue
            options["appId"] = apps[tile["app"]]
        missing = [n for n in tile.get("integrations", []) if n not in ids]
        if missing:
            problem(f"tile {tile['kind']}: integration {', '.join(missing)} does not exist")
            continue
        width, height = tile.get("size", [None, None])
        body = {"boardId": created["boardId"], "kind": tile["kind"], "options": options,
                "integrationIds": [ids[n]["id"] for n in tile.get("integrations", [])]}
        if width:
            body["size"] = {"width": width, "height": height}
        try:
            http("POST", f"{base}/api/boards/items", headers, body)
        except ApiError as error:
            problem(f"tile {tile['kind']}: {error}")


def main():
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit(".env not found")
    env = load_env(env_file)
    loaded = yaml.safe_load((ROOT / "config" / "arr.yml").read_text())
    if not isinstance(loaded, dict) or "homarr" not in loaded:
        sys.exit("config/arr.yml has no `homarr:` section")
    section = expand(loaded["homarr"], env)
    host = env.get("LAN_IP", "127.0.0.1")
    host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    base = f"http://{host}"

    print("Homarr")
    if not env.get("HOMARR_API_KEY"):
        print("  skipped: HOMARR_API_KEY is empty. Open http://%s, create the admin user, then Manage > Tools > API >\n"
              "  Authentication > new API key, put it in .env as HOMARR_API_KEY and run this script again." % host)
        return
    if wait_ready(base):
        headers = {"ApiKey": env["HOMARR_API_KEY"]}
        try:
            ids = integrations(base, headers, section.get("integrations") or [])
            board(base, headers, section["board"], ids)
        except (ApiError, KeyError) as error:
            problem(f"{error} (is HOMARR_API_KEY valid?)")

    print(f"\n{changes} change(s) {'needed' if CHECK else 'applied'}, {problems} problem(s).")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
