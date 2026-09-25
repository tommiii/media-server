#!/usr/bin/env python3
"""Configure Cleanuparr from the `cleanuparr:` section of arr.yml. Idempotent.

    ./scripts/apply_cleanuparr_config.py            apply
    ./scripts/apply_cleanuparr_config.py --check    show what would change, change nothing
    ./scripts/apply_cleanuparr_config.py --force    also rewrite the passwords / API keys of the client and the apps

Run it on the server, from the repository, once the containers are up. It talks to Cleanuparr on LAN_IP:11011
(see .env). Cleanuparr keeps its settings in its own database and has no config file, so this is how they
stay in this repository. Needs PyYAML: sudo apt install python3-yaml

What it does, in this order
  1. Account: on the first start creates the admin account from `auth:` (ARR_USERNAME / ARR_PASSWORD), then writes the
     API key into .env as CLEANUPARR_API_KEY and uses it from then on.
  2. General settings, the qBittorrent connection, and the Sonarr / Radarr instances (each connection is tested).
  3. Queue Cleaner: schedule, failed-import rules, and the stalled / slow rules (rules not in arr.yml are removed).
  4. Malware Blocker: schedule, and the blocklist file built from `blocked_files:`.
     The file goes to $BASE_DIR/services/cleanuparr/blocklist.txt, which the container sees as /config/blocklist.txt.
Settings that arr.yml does not mention are left as they are.
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
FORCE = "--force" in sys.argv
PORT = 11011
BLOCKLIST_IN_CONTAINER = "/config/blocklist.txt"
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


def camel(name):
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def camelize(node):
    """snake_case keys of arr.yml -> camelCase keys of the API (values are left alone)."""
    if isinstance(node, dict):
        return {camel(k): camelize(v) for k, v in node.items()}
    if isinstance(node, list):
        return [camelize(v) for v in node]
    return node


def http(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(headers or {})
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
            detail = parsed.get("detail") or parsed.get("title") or raw[:200]
            extra = "; ".join(m for v in (parsed.get("errors") or {}).values() for m in v)
            detail = f"{detail} ({extra})" if extra else detail
        except (ValueError, AttributeError, TypeError):
            detail = raw[:200]
        raise ApiError(f"HTTP {error.code} {detail}".strip()) from None
    except (urllib.error.URLError, OSError) as error:
        raise ApiError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from None
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw


def wait_ready(base, timeout=240):
    deadline = time.time() + timeout
    waited = False
    while True:
        try:
            urllib.request.urlopen(f"{base}/health", timeout=5)
            return True
        except urllib.error.HTTPError:
            return True  # it answered
        except (urllib.error.URLError, OSError):
            if time.time() > deadline:
                problem(f"Cleanuparr does not answer on {base} after {timeout}s: is the container up? "
                        "(docker compose ps cleanuparr; docker compose logs cleanuparr)")
                return False
            if not waited:
                print("  waiting for Cleanuparr...")
                waited = True
            time.sleep(3)


def pick(node, name):
    """Case-insensitive key lookup (the API answers in camelCase, but do not depend on it)."""
    return next((v for k, v in (node or {}).items() if k.lower() == name.lower()), None)


def overlay(current, wanted, path=""):
    """Copy of `current` with the keys of `wanted` applied; also returns the dotted keys that differ."""
    result, diffs = dict(current), []
    for key, value in wanted.items():
        here = f"{path}{key}"
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            result[key], sub = overlay(current[key], value, f"{here}.")
            diffs += sub
        elif current.get(key) != value:
            result[key] = value
            diffs.append(here)
    return result, diffs


# ---------------------------------------------------------------- account and API key
def authenticate(base, env, login):
    key = env.get("CLEANUPARR_API_KEY", "")
    if key:
        try:
            http("GET", f"{base}/api/configuration/general", {"X-Api-Key": key})
            return {"X-Api-Key": key}
        except ApiError:
            print("  the API key in .env is not accepted: signing in with the login instead")

    user, password = login.get("username"), login.get("password")
    if not user or not password:
        problem("no login for Cleanuparr: set ARR_USERNAME and ARR_PASSWORD in .env (at least 8 characters), "
                "or put the API key (Cleanuparr: Settings, Account) in .env as CLEANUPARR_API_KEY")
        return None

    status = http("GET", f"{base}/api/auth/status")
    if not pick(status, "setupCompleted"):
        note(f"admin account '{user}' created")
        if CHECK:
            return None  # nothing else can be looked at without an account
        try:
            http("POST", f"{base}/api/auth/setup/account", body={"username": user, "password": password})
            http("POST", f"{base}/api/auth/setup/complete")
        except ApiError as error:
            problem(f"account: {error} (username 3-50 characters, password 8-128)")
            return None

    try:
        answer = http("POST", f"{base}/api/auth/login", body={"username": user, "password": password})
    except ApiError as error:
        problem(f"cannot sign in to Cleanuparr as '{user}' ({error}). ARR_USERNAME/ARR_PASSWORD must be the login "
                "Cleanuparr really has; or put its API key in .env as CLEANUPARR_API_KEY")
        return None
    if pick(answer, "requiresTwoFactor"):
        problem("this Cleanuparr account has 2FA on, so a script cannot sign in: copy the API key "
                "(Settings, Account) into .env as CLEANUPARR_API_KEY")
        return None
    token = pick(pick(answer, "tokens"), "accessToken")
    fetched = http("GET", f"{base}/api/account/api-key", {"Authorization": f"Bearer {token}"})
    key = pick(fetched, "apiKey")
    if not key:
        problem("Cleanuparr did not return an API key")
        return None
    if CHECK:
        print("  would write CLEANUPARR_API_KEY to .env")
    else:
        set_env(ROOT / ".env", "CLEANUPARR_API_KEY", key)
        env["CLEANUPARR_API_KEY"] = key
        print("  wrote CLEANUPARR_API_KEY to .env")
    return {"X-Api-Key": key}


# ---------------------------------------------------------------- settings
def sync_config(base, headers, label, path, wanted):
    """GET the settings, apply what arr.yml says on top, PUT if something differs."""
    current = http("GET", f"{base}{path}", headers)
    body, diffs = overlay(current, camelize(wanted))
    if not diffs:
        print(f"  {label}: ok")
        return
    note(f"{label}: " + ", ".join(diffs))
    if not CHECK:
        http("PUT", f"{base}{path}", headers, body)


def upsert(base, headers, label, existing, wanted, secret, create_path, update_path):
    """Create or update one connection (download client or app instance). True if it was created or changed.

    The secret comes back masked, so it is only rewritten for a new entry, when something else changed, or with --force."""
    wanted = camelize(wanted)
    if existing is None:
        note(f"{label}: added")
        if not CHECK:
            http("POST", f"{base}{create_path}", headers, wanted)
        return not CHECK
    body, diffs = overlay(existing, {k: v for k, v in wanted.items() if k != secret})
    if FORCE:
        diffs.append(secret)
    if not diffs:
        print(f"  {label}: ok")
        return False
    note(f"{label}: " + ", ".join(diffs))
    if not CHECK:
        body[secret] = wanted[secret]  # never the masked value
        http("PUT", f"{base}{update_path.format(id=pick(existing, 'id'))}", headers, body)
    return not CHECK


def test_connection(base, headers, label, path, body):
    try:
        http("POST", f"{base}{path}", headers, camelize(body))
        print(f"  {label}: connection ok")
    except ApiError as error:
        problem(f"{label}: saved, but the connection test failed ({error})")


def strip_slash(entry, *keys):
    """The API answers URLs with a trailing slash; arr.yml does not have one."""
    entry = dict(entry)
    for key in keys:
        if isinstance(entry.get(key), str):
            entry[key] = entry[key].rstrip("/")
    return entry


def download_client(base, headers, cfg):
    print("Download client")
    wanted = {
        "enabled": True,
        "name": cfg["name"],
        "type_name": cfg.get("type_name", "qBittorrent"),
        "type": "Torrent",
        "host": cfg["host"],
        "username": cfg.get("username") or "",
        "password": cfg.get("password") or "",
    }
    if not wanted["password"]:
        problem("no qBittorrent password: set QBITTORRENT_PASSWORD in .env (apply_arr_config.py sets it in qBittorrent)")
        return
    clients = pick(http("GET", f"{base}/api/configuration/download_client", headers), "clients") or []
    existing = next((c for c in clients if pick(c, "name") == wanted["name"]), None)
    for other in clients:
        if other is not existing:
            print(f"  note: the client '{pick(other, 'name')}' is not in arr.yml; left alone")
    if existing is not None:
        existing = strip_slash(existing, "host", "externalUrl")
    if upsert(base, headers, wanted["name"], existing, wanted, "password",
              "/api/configuration/download_client", "/api/configuration/download_client/{id}"):
        keys = ("type_name", "type", "host", "username", "password")
        test_connection(base, headers, wanted["name"], "/api/configuration/download_client/test",
                        {k: wanted[k] for k in keys})


def arr_instances(base, headers, cfg):
    for app, spec in cfg.items():
        name = app.capitalize()
        print(name)
        if not spec.get("api_key"):
            problem(f"no API key for {app}: run ./scripts/apply_arr_config.py first (it fills {app.upper()}_API_KEY in .env)")
            continue
        instances = pick(http("GET", f"{base}/api/configuration/{app}", headers), "instances") or []
        existing = next((i for i in instances if pick(i, "name") == name), None)
        if existing is not None:
            existing = strip_slash(existing, "url", "externalUrl")
        wanted = {"enabled": True, "name": name, "url": spec["url"], "api_key": spec["api_key"],
                  "version": spec["version"]}
        if upsert(base, headers, name, existing, wanted, "apiKey",
                  f"/api/configuration/{app}/instances", f"/api/configuration/{app}/instances/{{id}}"):
            test_connection(base, headers, name, f"/api/configuration/{app}/instances/test",
                            {k: wanted[k] for k in ("url", "api_key", "version")})


def sync_rules(base, headers, kind, rules):
    label = f"{kind} rules"
    path = f"{base}/api/queue-rules/{kind}"
    current = http("GET", path, headers) or []
    by_name = {pick(r, "name").lower(): r for r in current}
    wanted = {r["name"].lower(): camelize(r) for r in rules}
    before = changes

    for name, rule in list(by_name.items()):  # first the ones to go: two rules may not overlap while one is being replaced
        if name not in wanted:
            note(f"{label}: '{pick(rule, 'name')}' removed (not in arr.yml)")
            if not CHECK:
                http("DELETE", f"{path}/{pick(rule, 'id')}", headers)
    for name, rule in wanted.items():
        existing = by_name.get(name)
        if existing is None:
            note(f"{label}: '{rule['name']}' added")
            if not CHECK:
                http("POST", path, headers, rule)
            continue
        body, diffs = overlay(existing, rule)
        if diffs:
            note(f"{label}: '{rule['name']}' " + ", ".join(diffs))
            if not CHECK:
                http("PUT", f"{path}/{pick(existing, 'id')}", headers, body)
    if changes == before:
        print(f"  {label}: ok")


def general(base, headers, cfg):
    print("General")
    sync_config(base, headers, "settings", "/api/configuration/general", cfg)


def queue_cleaner(base, headers, cfg):
    print("Queue Cleaner")
    cfg = dict(cfg)
    stall, slow = cfg.pop("stall_rules", []), cfg.pop("slow_rules", [])
    cfg["cron_expression"] = cfg.pop("cron")
    cfg["use_advanced_scheduling"] = True  # any cron expression is fine, not only what the basic mode can show
    sync_config(base, headers, "settings", "/api/configuration/queue_cleaner", cfg)
    sync_rules(base, headers, "stall", stall)
    sync_rules(base, headers, "slow", slow)


def malware_blocker(base, headers, cfg):
    print("Malware Blocker")
    cfg = dict(cfg)
    cfg["cron_expression"] = cfg.pop("cron")
    cfg["use_advanced_scheduling"] = True
    blocklist = {"enabled": True, "blocklist_type": "Blacklist", "blocklist_path": BLOCKLIST_IN_CONTAINER}
    cfg["sonarr"], cfg["radarr"] = dict(blocklist), dict(blocklist)
    sync_config(base, headers, "settings", "/api/configuration/malware_blocker", cfg)


def write_blocklist(env, patterns):
    """The list of arr.yml as a file for Cleanuparr: one pattern per line, no comments (it has no comment syntax)."""
    print("Blocklist")
    base_dir = env.get("BASE_DIR", "")
    if not base_dir or not (Path(base_dir) / "services").is_dir():
        problem("BASE_DIR is not set in .env or has no services/ folder: cannot write the blocklist file")
        return
    target = Path(base_dir) / "services" / "cleanuparr" / "blocklist.txt"
    text = "\n".join(patterns) + "\n"
    if target.exists() and target.read_text() == text:
        print(f"  {target}: ok")
        return
    note(f"{target} ({len(patterns)} patterns)")
    if not CHECK:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


# ---------------------------------------------------------------- main
def main():
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit(".env not found")
    env = load_env(env_file)
    loaded = yaml.safe_load((ROOT / "config" / "arr.yml").read_text())
    if not isinstance(loaded, dict) or "cleanuparr" not in loaded:
        sys.exit("config/arr.yml has no `cleanuparr:` section")
    # only the sections used here: the others (Plex, ...) need variables this script has no business with
    cfg = {name: expand(loaded.get(name), env) for name in ("auth", "blocked_files", "cleanuparr")}
    patterns = [p for p in (cfg.get("blocked_files") or []) if p]
    if not patterns:
        sys.exit("config/arr.yml: `blocked_files:` is empty, the Malware Blocker would have nothing to block")
    host = env.get("LAN_IP", "127.0.0.1")
    host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    base = f"http://{host}:{PORT}"
    section = cfg["cleanuparr"]

    print("Cleanuparr")
    write_blocklist(env, patterns)  # first: Cleanuparr refuses a Malware Blocker setting whose file does not exist
    if wait_ready(base):
        headers = authenticate(base, env, cfg.get("auth") or {})
        if headers:
            steps = (
                ("general", lambda: general(base, headers, section.get("general") or {})),
                ("download client", lambda: download_client(base, headers, section["download_client"])),
                ("arrs", lambda: arr_instances(base, headers, section.get("arrs") or {})),
                ("queue cleaner", lambda: queue_cleaner(base, headers, section["queue_cleaner"])),
                ("malware blocker", lambda: malware_blocker(base, headers, section["malware_blocker"])),
            )
            for label, run in steps:
                try:
                    run()
                except ApiError as error:
                    problem(f"{label}: {error}")

    print(f"\n{changes} change(s) {'needed' if CHECK else 'applied'}, {problems} problem(s).")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
