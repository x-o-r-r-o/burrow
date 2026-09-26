#!/usr/bin/python3
"""Burrow app updates: find newer versions of installed apps and install them safely.

Sources, in the order they're trusted for each app:
  1. Mac App Store       (App Store receipt; Apple's lookup API; updates open in the App Store)
  2. Homebrew            (apps installed with `brew install --cask`; `brew outdated`)
  3. Sparkle             (the app's own SUFeedURL appcast, like the app's built-in updater)
  4. Electron            (electron-updater's app-update.yml: GitHub releases or a generic feed)
  5. Homebrew catalog    (formulae.brew.sh: latest versions for ~7,000 apps, even not installed with brew)

Installing an update (Sparkle, Electron, catalog) never trusts the download blindly:
  - it must contain an app with the SAME bundle id as the installed one,
  - signed with a valid Apple code signature (codesign --verify --deep --strict),
  - by the SAME developer Team ID as the installed app,
  - newer than what's installed, and (catalog) matching Homebrew's SHA-256 when published.
The old version goes to the Trash as an undoable step, so an update can be rolled back.
Written for the system /usr/bin/python3 (3.9).
"""

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import engine
from engine import HOME, cached, load_state, save_state, sh, update_state

SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
CASK_API = "https://formulae.brew.sh/api/cask.json"
RESULTS = "updates.json"
IGNORE = "updates-ignore.json"
USER_AGENT = "Burrow/1.0 (macOS; app update checker)"
MAC_VERSION = platform.mac_ver()[0] or "0"
ARCH = platform.machine()  # arm64 / x86_64


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


PRERELEASE_RE = re.compile(r"^[-._ ]?(alpha|beta|preview|pre|rc|dev|a|b)\.?(\d*)", re.I)
PRERELEASE_RANK = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "pre": 3, "preview": 3, "rc": 4}


def version_key(v):
    """Comparable key: numeric parts, then release (1) above pre-release (0).
    "2.10.1" > "2.9"; "1.0" > "1.0b3"; "3.6.6-8b85519e" is a release; "5.3.2,1234" -> 5.3.2."""
    v = str(v or "").strip().lstrip("vV").split(",")[0].split(" ")[0]
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", v)
    if not m:
        return None
    nums = [int(x) for x in m.group(1).split(".")]
    rest = m.group(2)
    pre = PRERELEASE_RE.match(rest)
    if pre:
        rank = (0, PRERELEASE_RANK.get(pre.group(1).lower(), 0), int(pre.group(2) or 0))
    else:
        rev = re.match(r"^-(\d+)$", rest)  # "6.1.0-2": a packaging revision
        rank = (1, int(rev.group(1)) if rev else 0, 0)
    return nums, rank


def newer(candidate, installed):
    a, b = version_key(candidate), version_key(installed)
    if not a or not b:
        return False
    width = max(len(a[0]), len(b[0]))
    na = a[0] + [0] * (width - len(a[0]))
    nb = b[0] + [0] * (width - len(b[0]))
    return (na, a[1]) > (nb, b[1])


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def fetch(url, timeout=15, max_bytes=5 * 1024 * 1024, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes)


def fetch_json(url, timeout=15):
    return json.loads(fetch(url, timeout).decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Installed apps
# ---------------------------------------------------------------------------


def app_info(path):
    plist = engine.app_info_plist(path)
    try:
        with open(plist, "rb") as f:
            info = plistlib.load(f)
    except Exception:  # noqa: BLE001
        info = {}
    wrapped = os.path.exists(os.path.join(path, "Wrapper"))
    return {
        "name": os.path.basename(path)[:-4],
        "path": path,
        "bundle_id": info.get("CFBundleIdentifier"),
        "version": str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or ""),
        "build": str(info.get("CFBundleVersion") or ""),
        "feed": info.get("SUFeedURL") if isinstance(info.get("SUFeedURL"), str) else None,
        "mas": wrapped or os.path.exists(os.path.join(path, "Contents", "_MASReceipt", "receipt")),
        "ios": wrapped,
        "electron": os.path.exists(os.path.join(path, "Contents", "Resources", "app-update.yml")),
    }


# Apps that update themselves through a vendor updater; checking them here would
# only duplicate (or fight) that.
SELF_UPDATING = ("com.microsoft.", "com.google.Chrome", "com.adobe.", "com.jetbrains.", "com.setapp.")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def check_sparkle(app):
    """Newest eligible item of the app's Sparkle appcast."""
    data = fetch(app["feed"], headers={"User-Agent": "{}/{} Sparkle/2".format(app["name"], app["version"])})
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # Some feeds declare the wrong encoding; parse the text without the declaration
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", data.decode("utf-8", "replace"))
        root = ET.fromstring(text)
    best = None
    for it in root.iter("item"):
        def el(tag):
            node = it.find("{%s}%s" % (SPARKLE_NS, tag))
            return node.text.strip() if node is not None and node.text else None
        if el("channel"):
            continue  # beta/other channels: only what the app's updater offers by default
        enc = it.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        version = el("version") or enc.get("{%s}version" % SPARKLE_NS)
        short = el("shortVersionString") or enc.get("{%s}shortVersionString" % SPARKLE_NS) or version
        min_os = el("minimumSystemVersion")
        if min_os and newer(min_os, MAC_VERSION):
            continue
        hw = el("hardwareRequirements")
        if hw and ARCH not in hw:
            continue
        notes = el("releaseNotesLink") or (it.findtext("link") or "").strip() or None
        if version and (best is None or newer(version, best["build"])):
            best = {"build": version, "version": short, "url": enc.get("url"), "notes": notes}
    if not best:
        return None
    # Sparkle compares the build number (CFBundleVersion)
    if newer(best["build"], app["build"] or app["version"]):
        return dict(best, source="Sparkle", installable=True)
    return {"current": True}


def check_electron(app):
    """electron-updater: app-update.yml names a GitHub repo or a generic feed."""
    cfg = {}
    with open(os.path.join(app["path"], "Contents", "Resources", "app-update.yml")) as f:
        for line in f:
            m = re.match(r"^\s*(\w+):\s*['\"]?([^'\"#\n]+?)['\"]?\s*$", line)
            if m:
                cfg[m.group(1)] = m.group(2)
    provider = cfg.get("provider")
    if provider == "github" and cfg.get("owner") and cfg.get("repo"):
        rel = fetch_json("https://api.github.com/repos/{}/{}/releases/latest".format(cfg["owner"], cfg["repo"]))
        latest = rel.get("tag_name", "").lstrip("v")
        assets = rel.get("assets") or []
        url = pick_asset([(a.get("name", ""), a.get("browser_download_url")) for a in assets])
        notes = rel.get("html_url")
    elif provider == "generic" and cfg.get("url"):
        base = cfg["url"].rstrip("/") + "/"
        text = fetch(base + "latest-mac.yml").decode("utf-8", "replace")
        m = re.search(r"^version:\s*['\"]?([^'\"\n]+)", text, re.M)
        latest = m.group(1).strip() if m else ""
        files = re.findall(r"url:\s*['\"]?([^'\"\n]+)", text) or re.findall(r"^path:\s*['\"]?([^'\"\n]+)", text, re.M)
        url = pick_asset([(u, u if u.startswith("http") else base + u) for u in files])
        notes = None
    else:
        return None
    if latest and newer(latest, app["version"]):
        return {"version": latest, "url": url, "notes": notes, "source": "Electron", "installable": bool(url)}
    return {"current": True} if latest else None


def pick_asset(assets):
    """Best download for this Mac: native arch, else universal, never the other arch."""
    other = "x64" if ARCH == "arm64" else "arm64"
    good = [(n, u) for n, u in assets if u and n.lower().endswith((".zip", ".dmg")) and "blockmap" not in n.lower()]
    native = [a for a in good if ARCH in a[0].lower() or ("arm64" if ARCH == "arm64" else "x64") in a[0].lower()]
    universal = [a for a in good if "universal" in a[0].lower()]
    neutral = [a for a in good if other not in a[0].lower()]
    for group in (native, universal, neutral):
        zips = [u for n, u in group if n.lower().endswith(".zip")]
        if group:
            return zips[0] if zips else group[0][1]
    return None


def check_app_store(apps):
    """Apple's public lookup API, by bundle id, 50 at a time."""
    results = {}
    ids = [a["bundle_id"] for a in apps if a.get("bundle_id")]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        for entity in ("macSoftware", "software"):  # iPhone/iPad apps on Apple silicon use "software"
            try:
                data = fetch_json("https://itunes.apple.com/lookup?bundleId={}&entity={}".format(",".join(chunk), entity))
            except Exception:  # noqa: BLE001
                continue
            for r in data.get("results", []):
                results.setdefault(r.get("bundleId"), r)
    out = {}
    for a in apps:
        r = results.get(a.get("bundle_id"))
        if not r:
            continue
        if newer(r.get("version"), a["version"]):
            out[a["path"]] = {"version": r["version"], "source": "App Store", "installable": False,
                              "url": "macappstore://apps.apple.com/app/id{}".format(r.get("trackId")),
                              "adam_id": r.get("trackId"), "ios": a.get("ios", False),
                              "notes": r.get("trackViewUrl"), "release_notes": (r.get("releaseNotes") or "")[:600]}
        else:
            out[a["path"]] = {"current": True}
    return out


def mas_path():
    """The mas command-line tool for the App Store, if installed."""
    for p in ("/opt/homebrew/bin/mas", "/usr/local/bin/mas"):
        if os.access(p, os.X_OK):
            return p
    return None


def app_store_installable(u):
    """App Store updates are installable once mas is installed (not iPhone/iPad apps)."""
    return u.get("source") == "App Store" and bool(u.get("adam_id")) and not u.get("ios") and bool(mas_path())


MAS_INSTALL_COMMAND = "brew install mas"


def install_app_store(updates_list):
    """Update App Store apps with mas, all with ONE password prompt (mas needs admin
    rights and your Apple Account signed in to the App Store). Returns (done, failed)."""
    import shlex
    mas = mas_path()
    if not mas:
        raise UpdateError("mas isn't installed")
    todo = [u for u in updates_list if app_store_installable(u)]
    if not todo:
        return [], []
    script = "; ".join("{} update {} >/dev/null 2>&1; echo {}:$?".format(shlex.quote(mas), int(u["adam_id"]), int(u["adam_id"])) for u in todo)
    ok, out = engine.run_as_admin(script, "Burrow needs your password to install App Store updates.")
    if ok is None:
        raise UpdateError("cancelled")
    done, failed = [], []
    for u in todo:
        now = app_info(u["path"])["version"]
        if not newer(u["version"], now):
            done.append("{} updated to {}".format(u["name"], now))
        else:
            failed.append("{}: the App Store didn't update it (is your Apple Account signed in, and did it buy this app?)".format(u["name"]))
    return done, failed


def brew_path():
    for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.access(p, os.X_OK):
            return p
    return None


def check_homebrew():
    """Casks installed with Homebrew that have a newer version ({app file name: info})."""
    brew = brew_path()
    if not brew:
        return {}
    out = subprocess.run([brew, "outdated", "--cask", "--greedy", "--json=v2"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1"), timeout=120).stdout
    try:
        casks = json.loads(out or b"{}").get("casks", [])
    except ValueError:
        return {}
    try:
        index = catalog_index()
    except Exception:  # noqa: BLE001 — offline and no catalog yet
        return {}
    result = {}
    for c in casks:
        for app_name, meta in index.items():
            if meta["token"] == c["name"]:
                result[app_name] = {"version": c["current_version"], "token": c["name"], "source": "Homebrew", "installable": True}
    return result


def installed_casks():
    tokens = set()
    for room in engine.CASKROOMS:
        tokens.update(t for t in engine._listdir(room) if not t.startswith("."))
    return tokens


def catalog_index():
    """App file name -> {token, version, url, sha256, auto_updates} from Homebrew's
    public cask catalog. Downloaded at most once a day (it's ~19 MB)."""
    def compute():
        data = json.loads(fetch(CASK_API, timeout=60, max_bytes=80 * 1024 * 1024).decode("utf-8"))
        index = {}
        for c in data:
            version = str(c.get("version") or "")
            if not version or version == "latest":
                continue
            for art in c.get("artifacts") or []:
                if isinstance(art, dict) and "app" in art:
                    for a in art["app"]:
                        name = a if isinstance(a, str) else (a.get("target") if isinstance(a, dict) else None)
                        if isinstance(name, str) and name.endswith(".app"):
                            index.setdefault(os.path.basename(name), {
                                "token": c["token"], "version": version, "url": c.get("url"),
                                "sha256": c.get("sha256") if c.get("sha256") not in (None, "no_check") else None,
                                "homepage": c.get("homepage"),
                                # bundle ids the cask names (quit/zap/uninstall): proves which app it is
                                "bids": sorted(set(re.findall(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9_-]+){2,}", json.dumps(c.get("artifacts") or []))))[:40],
                            })
        return index
    return cached("cask-index-v2", 86400, compute)


def check_catalog(app, index):
    meta = index.get(os.path.basename(app["path"]))
    if not meta:
        return None
    # Same file name isn't enough ("Helium.app" is two different apps): the cask must
    # name this app's bundle id somewhere.
    bid = (app.get("bundle_id") or "").lower()
    if not bid or not any(b.lower() == bid or b.lower().startswith(bid + ".") for b in meta.get("bids") or []):
        return None
    latest = meta["version"].split(",")[0]
    # Some casks version by build number; only trust versions shaped like the app's
    if not re.match(r"^\d", latest) or not re.match(r"^\d", app["version"]):
        return None
    if len(latest.split(".")) == 1 and len(app["version"].split(".")) > 1:
        return None
    if latest in (app["version"], app["build"]):
        return {"current": True}
    if newer(latest, app["version"]):
        return {"version": latest, "url": meta["url"], "sha256": meta.get("sha256"), "notes": meta.get("homepage"),
                "source": "Homebrew catalog", "installable": bool(meta.get("url")), "token": meta["token"]}
    return {"current": True}


# ---------------------------------------------------------------------------
# Checking everything
# ---------------------------------------------------------------------------


def check_all():
    """Check every installed app. Returns the saved results."""
    apps = [app_info(a["path"]) for a in engine.list_apps()]
    apps = [a for a in apps if a["bundle_id"] and a["version"] and not a["bundle_id"].startswith(SELF_UPDATING)
            and not a["name"].startswith("Alfred ")]
    found, current, errors = {}, set(), {}

    store = [a for a in apps if a["mas"]]
    for path, r in check_app_store(store).items():
        (current.add(path) if r.get("current") else found.__setitem__(path, r))

    casks = installed_casks()
    brew_updates = check_homebrew() if casks else {}
    try:
        index = catalog_index()
    except Exception as e:  # noqa: BLE001
        index, errors["catalog"] = {}, str(e)

    def one(app):
        if app["mas"]:
            return app["path"], None
        name = os.path.basename(app["path"])
        meta = index.get(name)
        if meta and meta["token"] in casks:
            return app["path"], brew_updates.get(name) or {"current": True}
        try:
            if app["feed"] and app["feed"].startswith("https://"):
                r = check_sparkle(app)
                if r:
                    return app["path"], r
            if app["electron"]:
                r = check_electron(app)
                if r:
                    return app["path"], r
        except Exception as e:  # noqa: BLE001 — a broken feed shouldn't stop the others
            errors[app["name"]] = str(e)[:120]
        return app["path"], check_catalog(app, index) if index else None

    with ThreadPoolExecutor(max_workers=12) as pool:
        for path, r in pool.map(one, [a for a in apps if not a["mas"]]):
            if r is None:
                continue
            (current.add(path) if r.get("current") else found.__setitem__(path, r))

    by_path = {a["path"]: a for a in apps}
    updates = []
    for path, r in found.items():
        a = by_path[path]
        updates.append(dict(r, name=a["name"], path=path, bundle_id=a["bundle_id"], installed=a["version"]))
    updates.sort(key=lambda u: u["name"].lower())
    unknown = len(apps) - len(found) - len(current)
    result = {"time": time.time(), "updates": updates, "current": len(current), "unknown": unknown,
              "checked": len(apps), "errors": errors}
    save_state(RESULTS, result)
    return result


def visible_updates(result=None):
    """Updates minus the ones you chose to skip or ignore."""
    result = result or load_state(RESULTS)
    ignore = load_state(IGNORE)
    out = []
    for u in result.get("updates", []):
        rule = ignore.get(u["bundle_id"])
        if rule == "*" or rule == u["version"]:
            continue
        if not os.path.exists(u["path"]) or not newer(u["version"], app_info(u["path"])["version"]):
            continue  # updated since the check
        if u.get("source") == "App Store":
            u = dict(u, installable=app_store_installable(u))
        out.append(u)
    return out


def set_ignore(bundle_id, rule):
    """rule: a version to skip, "*" to ignore the app, None to stop ignoring."""
    def change(d):
        if rule is None:
            d.pop(bundle_id, None)
        else:
            d[bundle_id] = rule
        return d
    update_state(IGNORE, change)


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------


class UpdateError(Exception):
    pass


def signing(app_path):
    """(valid signature?, team id, bundle id) of an app bundle."""
    ok = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120).returncode == 0
    out = subprocess.run(["/usr/bin/codesign", "-dv", app_path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30).stderr.decode("utf-8", "replace")
    team = re.search(r"TeamIdentifier=(\S+)", out)
    ident = re.search(r"Identifier=(\S+)", out)
    team = team.group(1) if team and team.group(1) != "not" else None
    return ok, team, ident.group(1) if ident else None


def check_runs_here(app_path):
    """The new version must support this macOS and this Mac's processor."""
    try:
        with open(engine.app_info_plist(app_path), "rb") as f:
            info = plistlib.load(f)
    except Exception:  # noqa: BLE001
        return
    min_os = info.get("LSMinimumSystemVersion")
    if min_os and newer(str(min_os), MAC_VERSION):
        raise UpdateError("the new version needs macOS {} or later".format(min_os))
    exe = info.get("CFBundleExecutable")
    if exe:
        binary = os.path.join(app_path, "Contents", "MacOS", exe)
        archs = subprocess.run(["/usr/bin/lipo", "-archs", binary], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().split()
        if archs and ARCH not in archs and not (ARCH == "arm64" and "x86_64" in archs and rosetta_installed()):
            raise UpdateError("the new version doesn't run on this Mac's processor ({})".format(", ".join(archs)))


def rosetta_installed():
    return os.path.exists("/Library/Apple/usr/libexec/oah/libRosettaRuntime")


def gatekeeper_ok(app_path):
    """Gatekeeper accepts the app (Developer ID signed and notarized, or App Store)."""
    return subprocess.run(["/usr/sbin/spctl", "--assess", "--type", "execute", app_path],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120).returncode == 0


def download(url, dest, sha256=None, max_bytes=4 * 1024 ** 3):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise UpdateError("download is unexpectedly large")
            h.update(chunk)
            f.write(chunk)
    if sha256 and h.hexdigest() != sha256.lower():
        raise UpdateError("the download doesn't match Homebrew's checksum")
    return dest


def extract_app(archive, workdir, bundle_id):
    """Find the app with `bundle_id` inside a .zip, .dmg or .tar.* download.
    Returns (path to a copy of the app in workdir, cleanup function)."""
    low = archive.lower()
    root = os.path.join(workdir, "extracted")
    os.makedirs(root)
    mount = None
    if low.endswith(".dmg") or _is_dmg(archive):
        mount = os.path.join(workdir, "mnt")
        os.makedirs(mount)
        res = subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, archive],
                             input=b"Y\n", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
        if res.returncode != 0:
            raise UpdateError("couldn't open the disk image")
        search = mount
    elif low.endswith(".zip"):
        if subprocess.run(["/usr/bin/ditto", "-x", "-k", archive, root], stderr=subprocess.DEVNULL, timeout=600).returncode != 0:
            raise UpdateError("couldn't unzip the download")
        search = root
    elif re.search(r"\.tar(\.\w+)?$|\.tgz$", low):
        if subprocess.run(["/usr/bin/tar", "-xf", archive, "-C", root], stderr=subprocess.DEVNULL, timeout=600).returncode != 0:
            raise UpdateError("couldn't unpack the download")
        search = root
    elif low.endswith(".pkg"):
        raise UpdateError("this update is an installer package; update it from the app or its website")
    else:
        raise UpdateError("unknown download format")

    def cleanup():
        if mount:
            subprocess.run(["/usr/bin/hdiutil", "detach", "-force", mount], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        for base, dirs, _ in os.walk(search):
            for d in list(dirs):
                if d.endswith(".app"):
                    candidate = os.path.join(base, d)
                    if app_info(candidate)["bundle_id"] == bundle_id:
                        staged = os.path.join(workdir, "new", d)
                        os.makedirs(os.path.dirname(staged))
                        subprocess.run(["/usr/bin/ditto", candidate, staged], check=True, timeout=600)
                        return staged, cleanup
                    dirs.remove(d)  # don't look inside other apps
            if base.count(os.sep) - search.count(os.sep) > 3:
                dirs[:] = []
        raise UpdateError("the download doesn't contain this app")
    except Exception:
        cleanup()
        raise


def _is_dmg(path):
    try:
        with open(path, "rb") as f:
            f.seek(-512, os.SEEK_END)
            return f.read(4) == b"koly"
    except OSError:
        return False


def prepare(update, app=None, log=lambda msg: None):
    """Download and verify an update without installing it.
    Returns (path of the verified new app, its info, cleanup function)."""
    app = app or app_info(update["path"])
    if not update.get("installable") or not update.get("url"):
        raise UpdateError("Burrow can't install this update itself")
    if not update["url"].startswith("https://"):
        raise UpdateError("the download isn't served over HTTPS")
    ok_old, team_old, _ = signing(app["path"])
    work = tempfile.mkdtemp(prefix="burrow-update-", dir=engine.CACHE_DIR)
    name = re.sub(r"[^\w.\-]", "_", os.path.basename(update["url"].split("?")[0])) or "download"
    try:
        log("Downloading {} {}…".format(app["name"], update["version"]))
        archive = download(update["url"], os.path.join(work, name), update.get("sha256"))
    except UpdateError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(work, ignore_errors=True)
        raise UpdateError("download failed: {}".format(e))

    try:
        staged, cleanup = extract_app(archive, work, app["bundle_id"])
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    try:
        ok, team, _ = signing(staged)
        if not ok:
            raise UpdateError("the new version's code signature isn't valid")
        if team_old and team != team_old:
            raise UpdateError("the new version is signed by a different developer ({} instead of {})".format(team or "nobody", team_old))
        if not team_old and not team:
            raise UpdateError("neither version is signed by a known developer, so it can't be verified")
        new = app_info(staged)
        if not newer(new["build"] or new["version"], app["build"] or app["version"]) and not newer(new["version"], app["version"]):
            raise UpdateError("the download isn't newer than what's installed")
        if gatekeeper_ok(app["path"]) and not gatekeeper_ok(staged):
            raise UpdateError("macOS Gatekeeper rejects the new version (not notarized by Apple)")
        check_runs_here(staged)

    except Exception:
        cleanup()
        raise
    return staged, new, cleanup


class _AppLock:
    def __init__(self, path):
        import fcntl
        self.fd = open(os.path.join(engine.CACHE_DIR, ".update-" + hashlib.sha1(path.encode()).hexdigest()[:12] + ".lock"), "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fd.close()
            raise UpdateError("it's already being updated")

    def release(self):
        import fcntl
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def install(update, relaunch=True, log=lambda msg: None, unattended=False):
    """Download, verify and install one update. Returns a message; raises UpdateError.
    unattended (the daily run): never quit an app; skip it if it's open by then."""
    os.makedirs(engine.CACHE_DIR, exist_ok=True)
    lock = _AppLock(update["path"])
    try:
        return _install(update, relaunch, log, unattended)
    finally:
        lock.release()


def _install(update, relaunch=True, log=lambda msg: None, unattended=False):
    app = app_info(update["path"])
    if not os.path.exists(app["path"]):
        raise UpdateError("the app is no longer installed")
    if update["source"] == "App Store":
        done, failed = install_app_store([update])
        if failed:
            raise UpdateError(failed[0].split(": ", 1)[1])
        return done[0]
    if update["source"] == "Homebrew":
        brew = brew_path()
        res = subprocess.run([brew, "upgrade", "--cask", update["token"]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1", PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"), timeout=1800)
        if res.returncode != 0:
            tail = res.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] or ["brew failed"]
            raise UpdateError("brew upgrade failed: " + tail[0][:150])
        return "{} updated to {} with Homebrew".format(app["name"], update["version"])
    staged, new, cleanup = prepare(update, app, log)
    try:
        was_running = engine.app_is_running(app["path"])
        if was_running and unattended:
            raise UpdateError("{} is open, so it'll be updated another time".format(app["name"]))
        if was_running:
            if not engine.quit_app(app["path"], app["bundle_id"]):
                raise UpdateError("{} didn't quit".format(app["name"]))
        label = "Update {} to {}".format(app["name"], new["version"])
        batch = engine.new_batch_id()
        # Old version to the Trash (undoable = roll back), new version into place
        needs_root = engine.needs_root(app["path"])
        if needs_root:
            failed = engine.admin_trash([app["path"]], label, batch, prompt="Burrow needs your password to update {}.".format(app["name"]))
        else:
            failed = engine.trash_paths([app["path"]], finder_fallback=False, label=label, batch_id=batch)
        if failed:
            raise UpdateError("couldn't move the old version aside")
        record = engine.find_trash_batch(batch)
        if not record:
            # Moved but not tracked (should never happen): find it in the Trash and track it
            guess = os.path.join(HOME, ".Trash", os.path.basename(app["path"]))
            if os.path.exists(guess):
                engine.record_trash_batch(label, {app["path"]: guess}, batch)
                record = engine.find_trash_batch(batch)
            if not record:
                raise UpdateError("couldn't keep track of the old version; look for it in the Trash")
        res = subprocess.run(["/usr/bin/ditto", staged, app["path"]], stderr=subprocess.PIPE, timeout=600)
        if res.returncode != 0 and needs_root:
            import shlex
            engine.run_as_admin("/usr/bin/ditto {} {}".format(shlex.quote(staged), shlex.quote(app["path"])),
                                "Burrow needs your password to update {}.".format(app["name"]))
        installed = app_info(app["path"])
        if installed["bundle_id"] != app["bundle_id"] or not signing(app["path"])[0]:
            # A partial or broken copy: remove it and put the old version back
            if os.path.lexists(app["path"]):
                engine.trash_paths([app["path"]], finder_fallback=False)
            engine.undo_trash_batch(record)
            raise UpdateError("installing failed, so the old version was put back")
        mark_replacement(batch)
        if was_running and relaunch:
            subprocess.run(["/usr/bin/open", app["path"]], stderr=subprocess.DEVNULL)
        return "{} updated to {}".format(app["name"], new["version"])
    finally:
        cleanup()


def mark_replacement(batch_id):
    """Undoing an update must first move the NEW version out of the way."""
    def change(d):
        for b in d.get("batches", []):
            if b.get("id") == batch_id:
                for it in b["items"]:
                    it["replace"] = True
        return d
    update_state(engine.TRASH_HISTORY, change)



WF_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Automatic checks (a LaunchAgent that runs this file once a day)
# ---------------------------------------------------------------------------

AGENT_LABEL = "io.github.burrow-alfred.updates"
AGENT_PATH = os.path.join(HOME, "Library", "LaunchAgents", AGENT_LABEL + ".plist")


def sync_agent(mode, script_path):
    """Install/remove the daily check to match the setting (off / notify / install)."""
    want = mode in ("notify", "install")
    wanted = {
        "Label": AGENT_LABEL,
        "ProgramArguments": ["/bin/sh", "-c",
                             'if [ -f "$0" ]; then exec /usr/bin/python3 "$0" auto "$1"; '
                             'else /bin/launchctl bootout gui/$(id -u)/{0} 2>/dev/null; rm -f "$HOME/Library/LaunchAgents/{0}.plist"; fi'.format(AGENT_LABEL),
                             script_path, mode],
        "StartCalendarInterval": {"Hour": 10, "Minute": 30},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "EnvironmentVariables": {"alfred_workflow_cache": engine.CACHE_DIR, "alfred_workflow_bundleid": engine.BUNDLE_ID},
    }
    try:
        with open(AGENT_PATH, "rb") as f:
            current = plistlib.load(f)
    except Exception:  # noqa: BLE001
        current = None
    if want and current != wanted:
        os.makedirs(os.path.dirname(AGENT_PATH), exist_ok=True)
        with open(AGENT_PATH, "wb") as f:
            plistlib.dump(wanted, f)
        uid = os.getuid()
        subprocess.run(["/bin/launchctl", "bootout", "gui/{}/{}".format(uid, AGENT_LABEL)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["/bin/launchctl", "bootstrap", "gui/{}".format(uid), AGENT_PATH], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif not want and current is not None:
        subprocess.run(["/bin/launchctl", "bootout", "gui/{}/{}".format(os.getuid(), AGENT_LABEL)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.remove(AGENT_PATH)
        except OSError:
            pass


def notify(message):
    """Through Alfred when it's running (shows as Burrow), else a plain notification."""
    alfred = subprocess.run(["/usr/bin/pgrep", "-x", "Alfred"], stdout=subprocess.DEVNULL).returncode == 0
    if alfred:
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e",
                        'tell application id "com.runningwithcrayons.Alfred" to run trigger "notify" in workflow (item 1 of argv) with argument (item 2 of argv)',
                        "-e", "end run", engine.BUNDLE_ID, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "Burrow"', "-e", "end run", message],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def auto(mode):
    """The daily run: check, then notify, or install what's safe to install."""
    if not os.path.exists(__file__):
        sync_agent("off", __file__)  # Burrow was removed
        return
    # Follow the Workflow's Configuration as it is now, not as it was when the agent was made
    mode = _alfred_setting("auto_updates") or "off"
    if mode not in ("notify", "install"):
        sync_agent("off", __file__)
        return
    result = check_all()
    ups = visible_updates(result)
    if not ups:
        return
    installed, failed = [], []
    if mode == "install":
        for u in ups:
            if u.get("installable") and u["source"] not in ("Homebrew", "App Store") and not engine.app_is_running(u["path"]):
                try:
                    install(u, relaunch=False, unattended=True)
                    installed.append(u["name"])
                except Exception:  # noqa: BLE001
                    failed.append(u["name"])
    waiting = [u["name"] for u in ups if u["name"] not in installed]
    parts = []
    if installed:
        parts.append("Updated " + ", ".join(installed[:4]) + ("…" if len(installed) > 4 else ""))
    if waiting:
        parts.append("{} update{} available: {}".format(len(waiting), "" if len(waiting) == 1 else "s", ", ".join(waiting[:4]) + ("…" if len(waiting) > 4 else "")))
    notify(" · ".join(parts))


def history():
    """Past updates that can still be rolled back (their old version is in the Trash)."""
    out = []
    for b in reversed(load_state(engine.TRASH_HISTORY).get("batches", [])):
        if b.get("label", "").startswith("Update ") and any(os.path.lexists(i["to"]) for i in b["items"]):
            out.append({"id": b.get("id"), "label": b["label"], "time": b["time"]})
    return out


def window_state(check=False):
    """Everything the updater window shows, as JSON-ready data."""
    result = check_all() if check else load_state(RESULTS)
    ups = visible_updates(result)
    ignore = load_state(IGNORE)
    return {
        "checked": result.get("time"), "current": result.get("current", 0), "unknown": result.get("unknown", 0),
        "updates": [dict(u, size=None) for u in ups],
        "ignored": [{"bundle_id": k, "rule": v} for k, v in sorted(ignore.items())],
        "history": history(),
        "mas": bool(mas_path()), "brew": bool(brew_path()),
        "mode": os.environ.get("auto_updates") or _alfred_setting("auto_updates") or "off",
        "failure": result.get("failure"),
    }


def _alfred_setting(name):
    """A workflow setting from Alfred's prefs.plist (for runs outside Alfred)."""
    try:
        with open(os.path.join(WF_DIR, "prefs.plist"), "rb") as f:
            return plistlib.load(f).get(name)
    except Exception:  # noqa: BLE001
        return None


def cli(argv):
    """JSON commands for the updater window. Progress is printed as JSON lines."""
    import sys
    closed = []

    def say(obj):
        if closed:
            return
        try:
            print(json.dumps(obj), flush=True)
        except BrokenPipeError:
            closed.append(True)  # the window went away: keep working, stop reporting
            try:
                sys.stdout = open(os.devnull, "w")
            except OSError:
                pass
    cmd = argv[0] if argv else ""
    if cmd == "state":
        try:
            say(window_state(check="--check" in argv))
        except Exception as e:  # noqa: BLE001
            state = window_state()
            state["error"] = "The check failed: {}".format(e)
            say(state)
    elif cmd == "install":
        result = load_state(RESULTS)
        store = [u for u in visible_updates(result) if u["path"] in argv[1:] and u.get("source") == "App Store" and u.get("installable")]
        if store:
            for u in store:
                say({"path": u["path"], "stage": "working", "message": "Updating from the App Store…"})
            try:
                done, failed = install_app_store(store)
                for u in store:
                    msg = next((d for d in done if d.startswith(u["name"] + " ")), None)
                    say({"path": u["path"], "done": msg} if msg else {"path": u["path"], "error": next((f.split(": ", 1)[1] for f in failed if f.startswith(u["name"] + ":")), "not updated")})
            except Exception as e:  # noqa: BLE001
                for u in store:
                    say({"path": u["path"], "error": str(e)})
        for path in [p for p in argv[1:] if p not in {u["path"] for u in store}]:
            u = next((x for x in visible_updates(result) if x["path"] == path), None)
            if not u:
                say({"path": path, "error": "no update found for this app"})
                continue
            try:
                say({"path": path, "stage": "downloading"})
                msg = install(u, log=lambda m, p=path: say({"path": p, "stage": "working", "message": m}))
                say({"path": path, "done": msg})
            except Exception as e:  # noqa: BLE001
                say({"path": path, "error": str(e)})
    elif cmd == "skip":
        set_ignore(argv[1], argv[2])
        say({"ok": True})
    elif cmd == "ignore":
        set_ignore(argv[1], "*")
        say({"ok": True})
    elif cmd == "unignore":
        set_ignore(argv[1], None)
        say({"ok": True})
    elif cmd == "rollback":
        batch = next((b for b in load_state(engine.TRASH_HISTORY).get("batches", []) if b.get("id") == argv[1]), None)
        if not batch:
            say({"error": "that update can't be rolled back any more"})
        elif engine.batch_running_apps(batch) and "--quit" not in argv:
            say({"error": "{} is open. Quit it first".format(os.path.basename(engine.batch_running_apps(batch)[0])[:-4]), "running": True})
        else:
            for app in engine.batch_running_apps(batch):
                engine.quit_app(app, app_info(app)["bundle_id"])
            restored, skipped = engine.undo_trash_batch(batch)
            say({"ok": not skipped, "restored": len(restored), "skipped": len(skipped),
                 "done": "Rolled back" if restored and not skipped else None,
                 "error": None if not skipped else "{} couldn't be put back".format(len(skipped))})
    else:
        say({"error": "unknown command"})


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        cli(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "auto":
        auto(sys.argv[2] if len(sys.argv) > 2 else "notify")
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        r = check_all()
        print(json.dumps({"checked": r["checked"], "updates": [(u["name"], u["installed"], u["version"], u["source"]) for u in r["updates"]],
                          "current": r["current"], "unknown": r["unknown"], "errors": r["errors"]}, indent=2))
    else:
        print(__doc__)
