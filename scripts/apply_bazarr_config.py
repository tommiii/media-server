#!/usr/bin/env python3
"""Configure Bazarr (subtitles) from the `bazarr:` section of arr.yml. Idempotent.

    ./scripts/apply_bazarr_config.py            apply
    ./scripts/apply_bazarr_config.py --check    show what would change, change nothing

Run it on the server, from the repository, once the containers are up and ./scripts/apply_arr_config.py has filled the
Sonarr / Radarr API keys in .env. It talks to Bazarr on LAN_IP:6767 (see .env) with the API key Bazarr writes into
$BASE_DIR/services/bazarr/config/config.yaml on its first start. Needs PyYAML: sudo apt install python3-yaml

What it does, in this order
  1. Settings: login (form, from BAZARR_USERNAME / BAZARR_PASSWORD, else ARR_USERNAME / ARR_PASSWORD), the Sonarr and
     Radarr connections, the providers (one with an empty credential is left off) and everything under `settings:`.
  2. The languages profile: created or updated by name, its languages enabled, and made the default for new series and
     movies. Other profiles are left as they are.
  3. Series and movies that have no profile get this one (Bazarr then lists what they are missing and searches it).
  4. Checks that Bazarr reaches Sonarr and Radarr.
Settings that arr.yml does not mention are left as they are.
"""
import hashlib
import json
import re
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

ROOT = Path(__file__).resolve().parent.parent  # the repository root (this file lives in scripts/)
CHECK = "--check" in sys.argv
PORT = 6767
HI = {"allowed": "False", "required": "True", "excluded": "Excluded"}  # arr.yml -> Bazarr's profile item values
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
    print(("  would change: " if CHECK else "  changed:      ") + message)


def problem(message):
    global problems
    problems += 1
    print(f"  PROBLEM: {message}")


def http(method, url, headers=None, form=None):
    """Bazarr's API takes form fields (a list value = the same field repeated) and answers JSON."""
    data = urllib.parse.urlencode(form, doseq=True).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as error:
        raise ApiError(f"HTTP {error.code} {error.read().decode()[:200]}".strip()) from None
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
            urllib.request.urlopen(f"{base}/api/system/ping", timeout=5)
            return True
        except urllib.error.HTTPError:
            return True  # it answered
        except (urllib.error.URLError, OSError):
            if time.time() > deadline:
                problem(f"Bazarr does not answer on {base} after {timeout}s: is the container up? "
                        "(docker compose ps bazarr; docker compose logs bazarr)")
                return False
            if not waited:
                print("  waiting for Bazarr...")
                waited = True
            time.sleep(3)


def form_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [str(v) for v in value] or [""]  # Bazarr reads a single empty value as "empty list"
    return "" if value is None else str(value)


def api_key(env):
    config = Path(env.get("BASE_DIR", "")) / "services" / "bazarr" / "config" / "config.yaml"
    try:
        return (yaml.safe_load(config.read_text()) or {}).get("auth", {}).get("apikey")
    except OSError:
        problem(f"cannot read {config}: has Bazarr started at least once? (docker compose up -d bazarr)")
    return None


# ---------------------------------------------------------------- settings
def wanted_settings(cfg, login):
    """Everything arr.yml says, as {(section, key): value} with Bazarr's names."""
    wanted = {}
    for section, values in (cfg.get("settings") or {}).items():
        for key, value in (values or {}).items():
            wanted[(section, key)] = value
    for app in ("sonarr", "radarr"):
        for key, value in (cfg.get(app) or {}).items():
            wanted[(app, key)] = value
        wanted[("general", f"use_{app}")] = bool((cfg.get(app) or {}).get("apikey"))

    enabled = []
    for name, values in (cfg.get("providers") or {}).items():
        values = values or {}
        secrets = [k for k in values if k in ("username", "password", "api_key", "apikey", "token")]
        if any(not values[k] for k in secrets):
            print(f"  provider {name}: off (its credential is empty in .env)")
            continue
        enabled.append(name)
        for key, value in values.items():
            wanted[(name, key)] = value
    wanted[("general", "enabled_providers")] = enabled

    if login.get("username") and login.get("password"):
        wanted[("auth", "type")] = "form"
        wanted[("auth", "username")] = login["username"]
        wanted[("auth", "password")] = login["password"]
    return wanted


def sync_settings(base, headers, wanted):
    print("Settings")
    current = http("GET", f"{base}/api/system/settings", headers)
    diff = {}
    for (section, key), value in wanted.items():
        have = (current.get(section) or {}).get(key)
        if (section, key) == ("auth", "password"):
            # Bazarr keeps an md5 of the password and hashes what it is sent
            same = have == hashlib.md5(value.encode()).hexdigest()
        elif isinstance(value, list):
            same = sorted(map(str, have or [])) == sorted(map(str, value))
        else:
            same = have == value
        if not same:
            diff[f"settings-{section}-{key}"] = form_value(value)
    if not diff:
        print("  settings: ok")
        return
    note("settings: " + ", ".join(k.removeprefix("settings-").replace("-", ".", 1) for k in diff))
    if not CHECK:
        http("POST", f"{base}/api/system/settings", headers, diff)


def sync_profile(base, headers, spec):
    print("Languages profile")
    name, languages = spec["name"], spec["languages"]
    hi = HI.get(str(spec.get("hearing_impaired", "allowed")).lower())
    if hi is None:
        problem(f"profile.hearing_impaired must be one of {', '.join(HI)}")
        return None
    profiles = http("GET", f"{base}/api/system/languages/profiles", headers) or []
    existing = next((p for p in profiles if p["name"] == name), None)
    profile_id = existing["profileId"] if existing else max((p["profileId"] for p in profiles), default=0) + 1
    items = [{"id": i, "language": code, "audio_exclude": "False", "audio_only_include": "False", "hi": hi,
              "forced": "False"} for i, code in enumerate(languages, 1)]
    profile = {"profileId": profile_id, "name": name, "cutoff": None, "items": items, "mustContain": [],
               "mustNotContain": [], "originalFormat": None, "tag": None}

    form = {}
    enabled = {x["code2"] for x in http("GET", f"{base}/api/system/languages", headers) or [] if x.get("enabled")}
    if not set(languages) <= enabled:
        note("languages enabled: " + ", ".join(sorted(set(languages) - enabled)))
        form["languages-enabled"] = sorted(enabled | set(languages))
    if existing is None:
        note(f"profile '{name}' created ({', '.join(languages)})")
    elif {k: existing.get(k) for k in profile} != profile:
        note(f"profile '{name}' updated ({', '.join(languages)}, hearing impaired: {spec.get('hearing_impaired')})")
    else:
        print(f"  profile '{name}': ok")
        profile = None
    if profile is not None:
        # the API replaces the whole list: the other profiles are sent back unchanged so that none is deleted
        form["languages-profiles"] = json.dumps([p for p in profiles if p["profileId"] != profile_id] + [profile])
    if form and not CHECK:
        http("POST", f"{base}/api/system/settings", headers, form)
    return profile_id


def default_profile(base, headers, profile_id):
    current = http("GET", f"{base}/api/system/settings", headers)["general"]
    wanted = {"serie_default_enabled": True, "serie_default_profile": profile_id,
              "movie_default_enabled": True, "movie_default_profile": profile_id}
    diff = {f"settings-general-{k}": form_value(v) for k, v in wanted.items() if str(current.get(k)) != str(v)}
    if not diff:
        print("  default for new series and movies: ok")
        return
    note("default for new series and movies")
    if not CHECK:
        http("POST", f"{base}/api/system/settings", headers, diff)


def assign_missing(base, headers, profile_id, path, id_key, form_key, label):
    items = (http("GET", f"{base}{path}", headers) or {}).get("data", [])
    without = [x[id_key] for x in items if x.get("profileId") is None]
    if not without:
        print(f"  {label} without a profile: none")
        return
    note(f"profile given to {len(without)} {label} that had none")
    if not CHECK:
        http("POST", f"{base}{path}", headers, {form_key: without, "profileid": [str(profile_id)] * len(without)})


def check_arrs(base, headers, cfg):
    print("Connections")
    status = {}
    for _ in range(10):  # Bazarr connects in the background after its settings change
        status = (http("GET", f"{base}/api/system/status", headers) or {}).get("data", {})
        if all(status.get(f"{app}_version") for app in ("sonarr", "radarr") if (cfg.get(app) or {}).get("apikey")):
            break
        time.sleep(3)
    for app in ("sonarr", "radarr"):
        if not (cfg.get(app) or {}).get("apikey"):
            problem(f"no API key for {app}: run ./scripts/apply_arr_config.py first (it fills {app.upper()}_API_KEY in .env)")
        elif status.get(f"{app}_version"):
            print(f"  {app.capitalize()} {status[f'{app}_version']}: ok")
        elif not CHECK:
            problem(f"Bazarr cannot reach {app.capitalize()} on {cfg[app]['ip']}:{cfg[app]['port']}: is it up, and is "
                    f"{app.upper()}_API_KEY in .env right? (Bazarr: System, Logs)")


# ---------------------------------------------------------------- main
def main():
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit(".env not found")
    env = load_env(env_file)
    loaded = yaml.safe_load((ROOT / "config" / "arr.yml").read_text())
    if not isinstance(loaded, dict) or "bazarr" not in loaded:
        sys.exit("config/arr.yml has no `bazarr:` section")
    # only the sections used here: the others (Plex, ...) need variables this script has no business with
    cfg = {name: expand(loaded.get(name), env) for name in ("auth", "bazarr")}
    section = cfg["bazarr"]
    host = env.get("LAN_IP", "127.0.0.1")
    host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    base = f"http://{host}:{PORT}"

    print("Bazarr")
    if wait_ready(base):
        key = api_key(env)
        if key:
            headers = {"X-API-KEY": key}
            own, shared = section.get("login") or {}, cfg.get("auth") or {}
            login = {k: own.get(k) or shared.get(k) for k in ("username", "password")}
            try:
                sync_settings(base, headers, wanted_settings(section, login))
                profile_id = sync_profile(base, headers, section["profile"])
                if profile_id is not None:
                    default_profile(base, headers, profile_id)
                    if not CHECK:
                        time.sleep(2)  # the profile list is cached for a moment after a change
                    assign_missing(base, headers, profile_id, "/api/series", "sonarrSeriesId", "seriesid", "series")
                    assign_missing(base, headers, profile_id, "/api/movies", "radarrId", "radarrid", "movies")
                check_arrs(base, headers, section)
            except ApiError as error:
                problem(str(error))

    print(f"\n{changes} change(s) {'needed' if CHECK else 'applied'}, {problems} problem(s).")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
