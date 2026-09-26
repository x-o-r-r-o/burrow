#!/usr/bin/python3
"""Burrow engine: the scanning and maintenance logic behind the Alfred workflow.

Usable on its own:

    engine.py status                 system status as JSON
    engine.py clean-scan             cleanable caches/logs, streamed as JSON lines
    engine.py purge-scan             old build artifacts, streamed as JSON lines
    engine.py analyze PATH           sizes of PATH's children, streamed as JSON lines
    engine.py app-sizes OUT.json     sizes of installed apps
    engine.py optimize [ID...|--all] list maintenance tasks, or run some (IDs from the list)
    engine.py touchid [status|enable|disable]
    engine.py large [MIN_BYTES]      big files in your home folder (Spotlight; default 500 MB)
    engine.py leftovers              files left behind by apps that are no longer installed
    engine.py updates                check installed apps for updates
    engine.py startup                launch agents and daemons
    engine.py dupes [PATH...]        identical files, streamed as JSON lines
    engine.py trash PATH...

Nothing is ever deleted outright: cleaning moves items to the Trash.
Written for the system /usr/bin/python3 (3.9), so no 3.10+ syntax.
"""

import calendar
import ctypes
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HOME = os.path.expanduser("~")
BUNDLE_ID = os.environ.get("alfred_workflow_bundleid", "io.github.burrow-alfred")
CACHE_DIR = os.environ.get("alfred_workflow_cache") or os.path.join(
    HOME, "Library/Caches/com.runningwithcrayons.Alfred/Workflow Data", BUNDLE_ID
)
# UTF-8, or ps escapes non-ASCII paths ("café" -> "cafM-CM-)") and running apps go unnoticed.
TOOL_ENV = dict(os.environ, PATH="/usr/bin:/bin:/usr/sbin:/sbin", LC_ALL="en_US.UTF-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PREFETCHED = {}


def _run_raw(args, timeout=10):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=TOOL_ENV, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return b""


def prefetch(commands, timeout=10):
    """Run several commands in parallel; the next sh()/sh_plist() of each uses the result."""
    with ThreadPoolExecutor(max_workers=len(commands) or 1) as pool:
        for args, raw in zip(commands, pool.map(lambda a: _run_raw(a, timeout), commands)):
            _PREFETCHED[tuple(args)] = raw


def _raw(args, timeout):
    key = tuple(args)
    if key in _PREFETCHED:
        return _PREFETCHED.pop(key)
    return _run_raw(args, timeout)


def sh(args, timeout=10):
    """Run a command and return stdout ('' on any failure)."""
    return _raw(args, timeout).decode("utf-8", "replace")


def sh_plist(args, timeout=10):
    raw = _raw(args, timeout)
    try:
        return plistlib.loads(raw) if raw else None
    except (plistlib.InvalidFileException, ValueError):
        return None


def cached(name, ttl, compute):
    """compute() once per `ttl` seconds, shared across runs through the cache folder."""
    path = state_path("memo-" + name + ".json")
    try:
        if time.time() - os.path.getmtime(path) < ttl:
            with open(path) as f:
                return json.load(f)["value"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        value = compute()
    except Exception:
        # Offline or failing: use the last good value rather than failing (or retrying
        # the network on every keystroke); try again after a short while.
        try:
            with open(path) as f:
                old = json.load(f)["value"]
            os.utime(path, (time.time(), time.time() - ttl + min(ttl, 1800)))
            return old
        except (OSError, ValueError, KeyError):
            raise
    save_state("memo-" + name + ".json", {"value": value})
    return value


def format_bytes(n):
    n = float(n or 0)
    if n < 1:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return "{:.0f} B".format(n) if i == 0 else "{:.1f} {}".format(n, units[i])


# File-provider folders (iCloud Drive, Dropbox, Google Drive…): walking them can
# trigger downloads or hang on disconnected accounts, and their files are online.
CLOUD_FOLDERS = {"CloudStorage", "Mobile Documents"}


def dir_size(path, same_device=True):
    """Allocated size of a file or directory tree, like `du -sk` but in bytes.
    Doesn't follow symlinks and (by default) stays on the starting volume."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    total = st.st_blocks * 512
    if not os.path.isdir(path) or os.path.islink(path):
        return total
    dev = st.st_dev
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                # Listing can fail part-way (timeouts on cloud or network folders).
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                est = entry.stat(follow_symlinks=False)
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            total += est.st_blocks * 512
            if is_dir and entry.name not in CLOUD_FOLDERS and (not same_device or est.st_dev == dev):
                stack.append(entry.path)
    return total


def emit_line(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def app_info_plist(app_path):
    """Info.plist of a Mac app, or of an iPhone/iPad app wrapped for Apple silicon
    (UPDF.app/Wrapper/UPDF.app/Info.plist)."""
    plist = os.path.join(app_path, "Contents", "Info.plist")
    if os.path.exists(plist):
        return plist
    wrapper = os.path.join(app_path, "Wrapper")
    for name in _listdir(wrapper):
        if name.endswith(".app") and os.path.exists(os.path.join(wrapper, name, "Info.plist")):
            return os.path.join(wrapper, name, "Info.plist")
    return plist


def running_process_names():
    return set(l.strip() for l in sh(["/bin/ps", "-Aco", "comm="]).splitlines() if l.strip())


def running_executables():
    """Full executable paths of every running process."""
    return [l.strip() for l in sh(["/bin/ps", "-Axo", "comm="]).splitlines() if l.strip()]


def app_is_running(app_path, executables=None):
    prefix = app_path.rstrip("/") + "/Contents/"
    return any(e.startswith(prefix) for e in (executables if executables is not None else running_executables()))


def installed_apps_by_bundle_id():
    """bundle id -> {"name", "path"} for installed apps (cached for 5 minutes)."""
    return cached("apps-by-bundle-id", 300, _installed_apps_by_bundle_id)


def _installed_apps_by_bundle_id():
    apps = {}
    dirs = ["/Applications", "/Applications/Utilities", os.path.join(HOME, "Applications"), "/System/Applications"]
    for sub in _listdir("/Applications"):  # vendor folders like "Adobe Photoshop 2025" or Setapp
        full = os.path.join("/Applications", sub)
        if not sub.endswith(".app") and not sub.startswith(".") and os.path.isdir(full) and full != "/Applications/Utilities":
            dirs.append(full)
    for d in dirs:
        for name in _listdir(d):
            if not name.endswith(".app"):
                continue
            path = os.path.join(d, name)
            bid = read_plist_key(app_info_plist(path), "CFBundleIdentifier")
            if bid and bid not in apps:
                apps[bid] = {"name": name[:-4], "path": path}
    return apps


def owning_app(entry_name, apps):
    """Match a cache/log folder name like 'com.brave.Browser.origin' to an app."""
    base = re.sub(r"\.(log|plist)$", "", entry_name)
    if base in apps:
        return apps[base]
    for app in apps.values():
        if app["name"].lower() == base.lower():
            return app
    best = None
    for bid in apps:
        if base.startswith(bid + ".") and (best is None or len(bid) > len(best)):
            best = bid
    return apps[best] if best else None


def state_path(name):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def load_state(name):
    try:
        with open(state_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class state_lock:
    """Serialises read-modify-write of shared state (undo history, stats…) between
    Burrow processes that can run at the same time (two actions, the menu bar)."""

    def __enter__(self):
        import fcntl
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.fd = open(os.path.join(CACHE_DIR, ".lock"), "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def update_state(name, change):
    """Atomically load a state file, apply change(data) -> data, and save it."""
    with state_lock():
        data = change(load_state(name))
        save_state(name, data)
        return data


def housekeeping(max_job_age=3 * 86400):
    """Remove Burrow's own stale files: old scan results (one per folder you ever
    analyzed), expired memos, forgotten per-app choices and undo entries whose
    items are no longer in the Trash. Runs at most once a day."""
    marker = os.path.join(CACHE_DIR, ".housekeeping")
    try:
        if time.time() - os.path.getmtime(marker) < 86400:
            return
    except OSError:
        pass
    os.makedirs(CACHE_DIR, exist_ok=True)
    open(marker, "w").close()
    now = time.time()
    jobs = os.path.join(CACHE_DIR, "jobs")
    for name in _listdir(jobs):
        path = os.path.join(jobs, name)
        try:
            if now - os.path.getmtime(path) > max_job_age and not name.endswith(".pid"):
                base = path.rsplit(".", 1)[0]
                if os.path.exists(base + ".done") or not os.path.exists(base + ".pid"):
                    os.remove(path)
            elif name.endswith(".pid") and now - os.path.getmtime(path) > max_job_age and os.path.exists(path[:-4] + ".done"):
                os.remove(path)
        except OSError:
            pass
    for name in _listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, name)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        stale = (name.startswith("memo-") and age > 2 * 86400) or \
                (name.startswith("uninstall-exclude-") and age > 30 * 86400) or \
                (name.startswith(".") and name.endswith(".tmp") and age > 3600)
        if stale:
            try:
                os.remove(path)
            except OSError:
                pass

    def prune(data):
        batches = data.get("batches", [])
        data["batches"] = [b for b in batches if any(os.path.lexists(i["to"]) for i in b["items"])]
        return data
    update_state(TRASH_HISTORY, prune)


def save_state(name, data):
    import tempfile
    os.makedirs(CACHE_DIR, exist_ok=True)  # the daily check / command line may run first
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix="." + name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, state_path(name))
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Trash and admin
# ---------------------------------------------------------------------------

TRASH_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "BurrowTrash")

# Backup when the compiled helper is missing (e.g. running from src/). It can't
# report where items landed, so those moves can't be undone.
TRASH_JXA = """
ObjC.import('Foundation');
function run(argv) {
  const fm = $.NSFileManager.defaultManager;
  for (const p of argv) {
    if (fm.fileExistsAtPath(p)) fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p), null, null);
  }
  return "{}";
}
"""

FINDER_TRASH = [
    "on run argv",
    "set theItems to {}",
    "repeat with p in argv",
    "set end of theItems to (POSIX file (p as text) as alias)",
    "end repeat",
    'tell application "Finder" to set movedItems to (delete theItems)',
    "if class of movedItems is not list then set movedItems to {movedItems}",
    "set out to {}",
    "repeat with m in movedItems",
    "set end of out to POSIX path of (m as alias)",
    "end repeat",
    "set AppleScript's text item delimiters to linefeed",
    "return out as text",
    "end run",
]

TRASH_HISTORY = "trash-history.json"
TRASH_HISTORY_KEEP = 20


def new_batch_id():
    return "{:.6f}-{}".format(time.time(), os.getpid())


def trash_paths(paths, finder_fallback=True, label=None, batch_id=None, moved_out=None):
    """Move paths to the Trash. Anything the file manager can't move is retried
    through Finder (which can ask for an admin password) in one batch. With a
    label, the move is recorded so it can be undone; calls sharing a batch_id
    become one undo step. Returns what failed."""
    paths = [p for p in paths if os.path.lexists(p)]
    moved = {}
    for start in range(0, len(paths), 500):
        chunk = paths[start:start + 500]
        helper_ok = False
        if os.access(TRASH_HELPER, os.X_OK):
            try:
                res = subprocess.run([TRASH_HELPER] + chunk, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                moved.update({k: v for k, v in json.loads(res.stdout.decode() or "").get("moved", {}).items() if v})
                helper_ok = res.returncode == 0
            except (OSError, ValueError, AttributeError):
                pass  # blocked by Gatekeeper or damaged: use the built-in method below
        if not helper_ok:
            remaining = [p for p in chunk if os.path.lexists(p) and p not in moved]
            if remaining and (label or moved_out is not None) and not any(os.path.islink(p) for p in remaining):
                # Undo needs to know where things went: Finder reports it (the JXA method can't)
                moved.update(_finder_trash(remaining))
                remaining = [p for p in remaining if os.path.lexists(p)]
            if remaining:
                subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", TRASH_JXA] + remaining, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Whatever still exists wasn't moved, whatever the script reported.
    failed = [p for p in paths if os.path.lexists(p)]
    # Finder resolves symlinks, so it would trash the link's target: never send it links.
    # (Finder's delete of items on network/FAT volumes may skip the Trash: never there either.)
    via_finder = [p for p in failed if not os.path.islink(p) and not p.startswith("/Volumes/")]
    if finder_fallback and via_finder:
        moved.update(_finder_trash(via_finder))
        failed = [p for p in failed if os.path.lexists(p)]
    moved = {k: v for k, v in moved.items() if not os.path.lexists(k)}
    if moved_out is not None:
        moved_out.update(moved)
    if label and moved:
        record_trash_batch(label, moved, batch_id)
    return failed


def record_trash_batch(label, moved, batch_id=None, root=False):
    """Add {original: location in Trash} to the undo history. root=True marks
    system-owned items, which are put back with the admin password."""
    new_items = [dict({"from": k, "to": v}, **({"root": True} if root else {})) for k, v in moved.items()]

    def change(data):
        history = data.get("batches", [])
        existing = next((b for b in history if batch_id and b.get("id") == batch_id), None)
        if existing:
            existing["items"] += new_items
            existing["time"] = time.time()
            history.remove(existing)
            history.append(existing)
        else:
            history.append({"id": batch_id or new_batch_id(), "time": time.time(), "label": label, "items": new_items})
        return {"batches": _cap_history(history)}
    update_state(TRASH_HISTORY, change)


def _cap_history(history):
    """Keep the last TRASH_HISTORY_KEEP batches, plus every app-update rollback
    (up to 50) so a week of cleaning never makes an update impossible to undo."""
    keep_ids = {id(b) for b in history[-TRASH_HISTORY_KEEP:]}
    updates = [b for b in history if any(i.get("replace") and i["from"].endswith(".app") for i in b["items"])][-50:]
    keep_ids.update(id(b) for b in updates)
    return [b for b in history if id(b) in keep_ids]


def _finder_trash(paths):
    """Move items to the Trash through Finder; returns {original: path in Trash}."""
    cmd = ["/usr/bin/osascript"]
    for line in FINDER_TRASH:
        cmd += ["-e", line]
    res = subprocess.run(cmd + paths, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    trashed = [l.rstrip("/") for l in res.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
    if res.returncode == 0 and len(trashed) == len(paths):
        return dict(zip(paths, trashed))
    return {}


def find_trash_batch(batch_id):
    return next((b for b in load_state(TRASH_HISTORY).get("batches", []) if b.get("id") == batch_id), None)


def last_trash_batch():
    batches = load_state(TRASH_HISTORY).get("batches", [])
    for batch in reversed(batches):
        if any(os.path.lexists(i["to"]) for i in batch["items"]):
            return batch
    return None


def batch_running_apps(batch):
    """Apps a batch would replace that are open right now (rolling back an update)."""
    return [it["from"] for it in batch["items"]
            if it.get("replace") and it["from"].endswith(".app") and os.path.lexists(it["from"]) and app_is_running(it["from"])]


def undo_trash_batch(batch):
    """Put a recorded batch back where it came from. Returns (restored, skipped).

    For "replace" items (rolling back an update or a cleaned database) the current
    version moves to the Trash first, and that move is itself recorded, so a rollback
    can be undone too. Root-owned items go back with one password prompt. Only the
    items that were actually put back (or can never be) leave the history, so a
    cancelled password prompt can simply be retried."""
    restored, skipped, done_items = [], [], []
    replaced = {}      # current versions moved aside -> where they went
    root_moves = []
    label = batch.get("label", "")
    for it in batch["items"]:
        src, dest = it["to"], it["from"]
        if not os.path.lexists(src):
            skipped.append(dest)  # emptied from the Trash, or already put back
            done_items.append(it)
            continue
        if it.get("root"):
            root_moves.append(it)
            continue
        if os.path.lexists(dest) and it.get("replace"):
            extras = [dest + x for x in ("-wal", "-shm", "-journal") if os.path.lexists(dest + x)]
            aside = {}
            if trash_paths([dest] + extras, finder_fallback=False, moved_out=aside):
                skipped.append(dest)
                continue
            replaced.update(aside)
        if os.path.lexists(dest):
            skipped.append(dest)  # something new lives there now (an app recreated its cache)
            done_items.append(it)
            continue
        if _move_back(src, dest):
            restored.append(dest)
            done_items.append(it)
        else:
            skipped.append(dest)
    if root_moves:
        import shlex
        trash = os.path.join(HOME, ".Trash")
        tag = new_batch_id().replace(".", "")[-8:]
        lines, asides = [], {}
        for i, it in enumerate(root_moves):
            src, dest = it["to"], it["from"]
            q = shlex.quote
            if it.get("replace") and os.path.lexists(dest):
                aside = os.path.join(trash, "{} replaced-{}-{}".format(os.path.basename(dest), tag, i))
                asides[dest] = aside
                lines.append("[ ! -e {a} ] && mv {d} {a}".format(a=q(aside), d=q(dest)))
            lines.append("mkdir -p {p} && [ ! -e {d} ] && mv {s} {d}".format(p=q(os.path.dirname(dest)), s=q(src), d=q(dest)))
        ok, _ = run_as_admin("; ".join(lines), "Burrow needs your password to put back system items.")
        for it in root_moves:
            dest = it["from"]
            if ok is not None and os.path.lexists(dest) and not os.path.lexists(it["to"]):
                restored.append(dest)
                done_items.append(it)
            else:
                skipped.append(dest)
        replaced.update({d: a for d, a in asides.items() if os.path.lexists(a)})
    if replaced:
        # The versions that were current before the rollback: undoable in turn
        record_trash_batch("Before undoing “{}”".format(label), replaced, root=bool(root_moves))
    done_ids = {id(x) for x in done_items}

    def change(data):
        out = []
        for b in data.get("batches", []):
            if _same_batch(b, batch):
                left = [it for it in b["items"] if not any(it["to"] == d["to"] for d in done_items)]
                if left:
                    b = dict(b, items=left)  # what didn't go back stays, to retry
                    out.append(b)
                continue
            out.append(b)
        return {"batches": out}
    update_state(TRASH_HISTORY, change)
    del done_ids
    return restored, skipped


def _move_back(src, dest):
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src, dest)
        return True
    except OSError:
        pass
    if os.path.islink(src):
        return False  # Finder would move the link's target instead
    # Privacy settings or ownership can block direct access to the Trash; Finder can do it.
    script = [
        "on run argv",
        'tell application "Finder" to set m to move (POSIX file (item 1 of argv) as alias) to (POSIX file (item 2 of argv) as alias)',
        "return POSIX path of (m as alias)",
        "end run",
    ]
    cmd = ["/usr/bin/osascript"]
    for line in script:
        cmd += ["-e", line]
    res = subprocess.run(cmd + [src, os.path.dirname(dest)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    landed = res.stdout.decode("utf-8", "replace").strip().rstrip("/")
    if res.returncode == 0 and landed and landed != dest:
        try:
            os.rename(landed, dest)
        except OSError:
            pass
    return os.path.lexists(dest)


def _same_batch(a, b):
    if a.get("id") and b.get("id"):
        return a["id"] == b["id"]
    return a["time"] == b["time"] and a["label"] == b["label"]


def run_as_admin(script, prompt):
    """Run a shell script as root via the standard macOS password prompt.
    Returns (ok, output). ok is None when the user cancelled."""
    res = subprocess.run(
        ["/usr/bin/osascript", "-e", "on run argv", "-e", "do shell script (item 1 of argv) with prompt (item 2 of argv) with administrator privileges", "-e", "end run", script, prompt],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    err = res.stderr.decode("utf-8", "replace")
    if res.returncode != 0 and "-128" in err:
        return None, "Cancelled"
    return res.returncode == 0, (res.stdout.decode("utf-8", "replace") + err).strip()


# ---------------------------------------------------------------------------
# System status
# ---------------------------------------------------------------------------


def cpu_ticks():
    """Per-core [user, system, idle, nice] tick counters from the Mach kernel."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        libc.mach_host_self.restype = ctypes.c_uint
        libc.host_processor_info.argtypes = [
            ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_int)), ctypes.POINTER(ctypes.c_uint),
        ]
        count = ctypes.c_uint()
        info = ctypes.POINTER(ctypes.c_int)()
        info_count = ctypes.c_uint()
        PROCESSOR_CPU_LOAD_INFO = 2
        if libc.host_processor_info(libc.mach_host_self(), PROCESSOR_CPU_LOAD_INFO, ctypes.byref(count), ctypes.byref(info), ctypes.byref(info_count)) != 0:
            return []
        return [[info[i * 4 + j] & 0xFFFFFFFF for j in range(4)] for i in range(count.value)]
    except (OSError, AttributeError):
        return []


def cpu_usage(prev, cur):
    per_core = []
    busy_total = all_total = 0
    for a, b in zip(prev, cur):
        d = [(b[j] - a[j]) & 0xFFFFFFFF for j in range(4)]
        total = sum(d)
        busy = total - d[2]
        per_core.append(100.0 * busy / total if total else 0.0)
        busy_total += busy
        all_total += total
    return (100.0 * busy_total / all_total if all_total else 0.0), per_core


def sysctl_values(names):
    out = {}
    for line in sh(["/usr/sbin/sysctl"] + names).splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def parse_mem_value(s):
    m = re.match(r"([\d.]+)([KMG])", s)
    if not m:
        return 0
    return float(m.group(1)) * {"K": 1024, "M": 1048576, "G": 1073741824}[m.group(2)]


def format_uptime(seconds):
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "{}d {}h".format(d, h)
    if h:
        return "{}h {}m".format(h, m)
    return "{}m".format(m)


def hardware_info(sysctls):
    cached = load_state("hardware.json")
    if cached.get("model") and time.time() - cached.get("cached_at", 0) < 86400:
        return cached
    model = sysctls.get("hw.model", "Mac")
    hw = sh_plist(["/usr/sbin/system_profiler", "SPHardwareDataType", "-xml"], timeout=20)
    try:
        items = hw[0]["_items"][0]
        model = items.get("machine_name") or model
    except (TypeError, KeyError, IndexError):
        pass
    info = {
        "model": model,
        "cpu_model": sysctls.get("machdep.cpu.brand_string", ""),
        "total_ram": format_bytes(int(sysctls.get("hw.memsize", "0") or 0)).replace(".0 ", " "),
        "os_version": "macOS " + sh(["/usr/bin/sw_vers", "-productVersion"]).strip(),
        "cached_at": time.time(),
    }
    save_state("hardware.json", info)
    return info


def memory_info(sysctls):
    text = sh(["/usr/bin/vm_stat"])
    page = 16384
    m = re.search(r"page size of (\d+) bytes", text)
    if m:
        page = int(m.group(1))
    pages = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip().rstrip(".")
            if v.isdigit():
                pages[k.strip().strip('"')] = int(v)
    total = int(sysctls.get("hw.memsize", "0") or 0)
    used = (
        pages.get("Anonymous pages", 0) - pages.get("Pages purgeable", 0)
        + pages.get("Pages wired down", 0) + pages.get("Pages occupied by compressor", 0)
    ) * page
    swap = sysctls.get("vm.swapusage", "")
    swap_total = parse_mem_value((re.search(r"total = ([\d.]+[KMG])", swap) or [None, "0M"])[1])
    swap_used = parse_mem_value((re.search(r"used = ([\d.]+[KMG])", swap) or [None, "0M"])[1])
    level = sysctls.get("kern.memorystatus_vm_pressure_level", "1")
    pressure = {"1": "normal", "2": "warning", "4": "critical"}.get(level, "normal")
    return {
        "used": used,
        "total": total,
        "used_percent": 100.0 * used / total if total else 0,
        "swap_used": swap_used,
        "swap_total": swap_total,
        "cached": pages.get("File-backed pages", 0) * page,
        "pressure": pressure,
    }


def volume_available(mount):
    """Free space including purgeable space, as Finder reports it (cached 1 min)."""
    def compute():
        out = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e",
                              'function run(a){ObjC.import("Foundation");const u=$.NSURL.fileURLWithPath(a[0]);const o=Ref();'
                              'u.getResourceValueForKeyError(o,$.NSURLVolumeAvailableCapacityForImportantUsageKey,null);'
                              'return String(ObjC.unwrap(o[0])||0)}', mount],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout.decode().strip()
        return int(out) if out.isdigit() else 0
    try:
        return cached("available-" + _normalize(mount or "root"), 60, compute) or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def disk_image_devices():
    """/dev/diskN names backed by mounted disk images (.dmg), which aren't real disks."""
    devices = set()
    info = sh_plist(["/usr/bin/hdiutil", "info", "-plist"], timeout=5) or {}
    for image in info.get("images", []):
        for ent in image.get("system-entities", []):
            dev = ent.get("dev-entry", "")
            if dev:
                devices.add(re.sub(r"s\d+$", "", dev))
    return devices


def disk_info():
    images = disk_image_devices()
    fstypes = {}
    for line in sh(["/sbin/mount"]).splitlines():
        m = re.match(r"^(\S+) on (.+) \(([^,)]+)", line)
        if m:
            fstypes[m.group(2)] = m.group(3)
    disks = []
    for line in sh(["/bin/df", "-kP"]).splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[0].startswith("/dev/"):
            continue
        mount = parts[5]
        if mount != "/" and not mount.startswith("/Volumes/"):
            continue
        if re.sub(r"s\d+$", "", parts[0]) in images or re.sub(r"(s\d+)+$", "", parts[0]) in images:
            continue
        total = int(parts[1]) * 1024
        avail = int(parts[3]) * 1024
        used = total - avail  # APFS volumes share their container's free space
        available = volume_available(mount) if mount == "/" else None
        disks.append({
            "available": available or avail,
            "mount": mount,
            "device": parts[0],
            "used": used,
            "total": total,
            "used_percent": 100.0 * used / total if total else 0,
            "fstype": fstypes.get(mount, ""),
            "external": mount.startswith("/Volumes/"),
        })
    return disks


def disk_io_bytes():
    read = write = 0
    for drv in sh_plist(["/usr/sbin/ioreg", "-c", "IOBlockStorageDriver", "-r", "-a"]) or []:
        stats = drv.get("Statistics") or {}
        read += stats.get("Bytes (Read)", 0)
        write += stats.get("Bytes (Write)", 0)
    return read, write


def network_bytes():
    counters = {}
    for line in sh(["/usr/sbin/netstat", "-ibn"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 10 and parts[2].startswith("<Link#"):
            try:
                # From the end: Ibytes Opkts Oerrs Obytes Coll (Address may be missing)
                counters[parts[0]] = (int(parts[-5]), int(parts[-2]))
            except ValueError:
                pass
    return counters


def interface_ips():
    ips = {}
    current = None
    for line in sh(["/sbin/ifconfig"]).splitlines():
        if line and not line[0].isspace():
            current = line.split(":", 1)[0]
        else:
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if m and current and not m.group(1).startswith("127.") and current not in ips:
                ips[current] = m.group(1)
    return ips


def proxy_info():
    text = sh(["/usr/sbin/scutil", "--proxy"])
    for kind in ("HTTPS", "HTTP", "SOCKS"):
        if re.search(r"\b{}Enable : 1".format(kind), text):
            host = re.search(r"\b{}Proxy : (\S+)".format(kind), text)
            port = re.search(r"\b{}Port : (\d+)".format(kind), text)
            return {"enabled": True, "type": kind, "host": "{}:{}".format(host.group(1) if host else "", port.group(1) if port else "")}
    return {"enabled": False, "type": "", "host": ""}


def battery_info():
    raw = sh_plist(["/usr/sbin/ioreg", "-rn", "AppleSmartBattery", "-a"])
    if not raw:
        return [], {}
    b = raw[0]
    data = b.get("BatteryData") or {}
    percent = b.get("CurrentCapacity", 0)
    if b.get("MaxCapacity", 100) not in (0, 100):
        percent = round(100.0 * b.get("CurrentCapacity", 0) / b["MaxCapacity"])
    design = b.get("DesignCapacity") or data.get("DesignCapacity") or 0
    full = b.get("AppleRawMaxCapacity") or data.get("NominalChargeCapacity") or data.get("FullChargeCapacity") or 0
    capacity = min(100, round(100.0 * full / design)) if design and full else 0

    if b.get("FullyCharged"):
        status = "charged"
    elif b.get("IsCharging"):
        status = "charging"
    elif b.get("ExternalConnected"):
        status = "not charging"
    else:
        status = "discharging"

    minutes = b.get("TimeRemaining")
    if b.get("IsCharging"):
        minutes = b.get("AvgTimeToFull", minutes)
    time_left = "{}:{:02d}".format(minutes // 60, minutes % 60) if minutes and minutes < 65535 and status in ("charging", "discharging") else ""

    amperage = b.get("Amperage", 0)
    if amperage > 0x7FFFFFFFFFFFFFFF:
        amperage -= 1 << 64
    # positive = charging, negative = draining
    battery_power = b.get("Voltage", 0) * amperage / 1e6
    adapter = (b.get("AdapterDetails") or {}).get("Watts", 0) if b.get("ExternalConnected") else 0
    telemetry = b.get("PowerTelemetryData") or {}
    # SystemLoad is what the Mac itself draws; SystemPowerIn also includes charging
    system_power = (telemetry.get("SystemLoad") or telemetry.get("SystemPowerIn") or 0) / 1000.0
    power_in = telemetry.get("SystemPowerIn", 0) / 1000.0

    battery = {
        "percent": percent,
        "status": status,
        "time_left": time_left,
        "health": "Normal" if not capacity or capacity >= 80 else "Service Recommended",
        "cycle_count": b.get("CycleCount", 0),
        "capacity": capacity,
    }
    battery["health"] = battery_condition() or battery["health"]
    return [battery], {"battery_power": battery_power, "adapter_power": adapter, "system_power": system_power, "power_in": power_in}


def battery_condition():
    """macOS's own verdict ("Normal", "Service Recommended"), cached for a day."""
    def compute():
        data = sh_plist(["/usr/sbin/system_profiler", "SPPowerDataType", "-xml"], timeout=20) or []
        try:
            for item in data[0]["_items"]:
                health = (item.get("sppower_battery_health_info") or {}).get("sppower_battery_health")
                if health:
                    return health
        except (IndexError, KeyError, TypeError):
            pass
        return ""
    return cached("battery-condition", 86400, compute)


def gpu_info():
    gpus = []
    for acc in sh_plist(["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator", "-a"]) or []:
        stats = acc.get("PerformanceStatistics") or {}
        usage = stats.get("Device Utilization %")
        if usage is None:
            continue
        gpus.append({"name": acc.get("model", "GPU"), "usage": float(usage), "core_count": acc.get("gpu-core-count", 0)})
    return gpus


def top_processes(n=5):
    """(the n busiest processes, how many processes are running)"""
    procs = []
    lines = sh(["/bin/ps", "-Aceo", "pcpu=,pmem=,comm=", "-r"]).splitlines()
    for line in lines[:n]:
        parts = line.split(None, 2)
        if len(parts) == 3:
            try:
                procs.append({"name": parts[2], "cpu": float(parts[0]), "memory": float(parts[1])})
            except ValueError:
                pass
    return procs, len(lines)


def health_score(cpu, mem, disks, batteries, cpu_temp=0.0, uptime_days=0):
    score = 100.0
    notes = []
    if uptime_days >= 14:
        score -= 5
        notes.append("restart recommended ({} days up)".format(int(uptime_days)))
    if cpu_temp > 85:
        score -= min(20, (cpu_temp - 85) * 2)
        notes.append("running hot")
    if cpu > 60:
        score -= min(20, (cpu - 60) * 0.5)
        notes.append("high CPU load")
    if mem["pressure"] == "critical":
        score -= 30
        notes.append("critical memory pressure")
    elif mem["pressure"] == "warning":
        score -= 15
        notes.append("memory pressure")

    if mem["total"] and mem["swap_used"] > mem["total"] * 0.25:
        score -= 10
        notes.append("heavy swap use")
    root = next((d for d in disks if d["mount"] == "/"), None)
    if root and root["used_percent"] > 85:
        score -= min(30, (root["used_percent"] - 85) * 2)
        notes.append("startup disk almost full")
    for b in batteries:
        if b["capacity"] and b["capacity"] < 80:
            score -= 10
            notes.append("battery needs service")
    score = int(max(0, min(100, round(score))))
    label = "Excellent" if score >= 90 else "Good" if score >= 75 else "Fair" if score >= 50 else "Poor"
    return score, label + (": " + ", ".join(notes) if notes else "")



# ---------------------------------------------------------------------------
# Temperatures and fans from the System Management Controller (no root needed)
# ---------------------------------------------------------------------------


class _SMCVers(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint8), ("minor", ctypes.c_uint8), ("build", ctypes.c_uint8),
                ("reserved", ctypes.c_uint8), ("release", ctypes.c_uint16)]


class _SMCPLimit(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint16), ("length", ctypes.c_uint16), ("cpu", ctypes.c_uint32),
                ("gpu", ctypes.c_uint32), ("mem", ctypes.c_uint32)]


class _SMCKeyInfo(ctypes.Structure):
    _fields_ = [("dataSize", ctypes.c_uint32), ("dataType", ctypes.c_uint32), ("dataAttributes", ctypes.c_uint8)]


class _SMCKeyData(ctypes.Structure):
    _fields_ = [("key", ctypes.c_uint32), ("vers", _SMCVers), ("pLimitData", _SMCPLimit), ("keyInfo", _SMCKeyInfo),
                ("result", ctypes.c_uint8), ("status", ctypes.c_uint8), ("data8", ctypes.c_uint8),
                ("data32", ctypes.c_uint32), ("bytes", ctypes.c_uint8 * 32)]


class SMC:
    READ_BYTES, READ_INDEX, READ_KEYINFO = 5, 8, 9

    def __init__(self):
        self.iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        io = self.iokit
        io.IOServiceMatching.restype = ctypes.c_void_p
        io.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io.IOServiceGetMatchingService.restype = ctypes.c_uint32
        io.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        io.IOServiceOpen.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        io.IOServiceClose.argtypes = [ctypes.c_uint32]
        io.IOObjectRelease.argtypes = [ctypes.c_uint32]
        io.IOConnectCallStructMethod.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t,
                                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        libc.mach_task_self.restype = ctypes.c_uint32
        service = io.IOServiceGetMatchingService(0, io.IOServiceMatching(b"AppleSMC"))
        if not service:
            raise OSError("AppleSMC not found")
        self.conn = ctypes.c_uint32()
        kr = io.IOServiceOpen(service, libc.mach_task_self(), 0, ctypes.byref(self.conn))
        io.IOObjectRelease(service)
        if kr != 0:
            raise OSError("Can't open AppleSMC ({})".format(kr))
        self.types = {}

    def close(self):
        self.iokit.IOServiceClose(self.conn.value)

    def _call(self, inp):
        out = _SMCKeyData()
        size = ctypes.c_size_t(ctypes.sizeof(_SMCKeyData))
        kr = self.iokit.IOConnectCallStructMethod(self.conn.value, 2, ctypes.byref(inp), ctypes.sizeof(_SMCKeyData), ctypes.byref(out), ctypes.byref(size))
        return out if kr == 0 and out.result == 0 else None

    @staticmethod
    def _code(key):
        return int.from_bytes(key.encode("latin1"), "big")

    def keys(self):
        count = self.read("#KEY") or 0
        found = []
        for i in range(int(count)):
            inp = _SMCKeyData()
            inp.data8 = self.READ_INDEX
            inp.data32 = i
            out = self._call(inp)
            if out:
                found.append(out.key.to_bytes(4, "big").decode("latin1"))
        return found

    def read(self, key):
        info = self.types.get(key)
        if info is None:
            inp = _SMCKeyData()
            inp.key = self._code(key)
            inp.data8 = self.READ_KEYINFO
            out = self._call(inp)
            if not out:
                return None
            info = self.types[key] = (out.keyInfo.dataSize, out.keyInfo.dataType.to_bytes(4, "big").decode("latin1"))
        size, kind = info
        inp = _SMCKeyData()
        inp.key = self._code(key)
        inp.data8 = self.READ_BYTES
        inp.keyInfo.dataSize = size
        out = self._call(inp)
        if not out:
            return None
        raw = bytes(out.bytes[:size])
        try:
            if kind == "flt ":
                return struct.unpack("<f", raw[:4])[0]
            if kind == "ui8 ":
                return raw[0]
            if kind == "ui16":
                return struct.unpack(">H", raw[:2])[0]
            if kind == "ui32":
                return struct.unpack(">I", raw[:4])[0]
            if kind == "fpe2":
                return struct.unpack(">H", raw[:2])[0] / 4.0
            if kind == "sp78":
                return struct.unpack(">h", raw[:2])[0] / 256.0
        except struct.error:
            return None
        return None


# Key prefixes: Apple silicon uses Tp/Te (CPU cores) and Tg (GPU); Intel Macs TC0*/TG0*.
CPU_TEMP_PREFIXES = ("Tp", "Te", "TC0", "TC1", "TC2", "TC3")
GPU_TEMP_PREFIXES = ("Tg", "TG0")
BATTERY_TEMP_PREFIXES = ("TB",)
SMC_CACHE_VERSION = 2  # bump when the set of cached sensor keys changes


def sensors():
    """CPU/GPU temperature (°C, averaged over the sensors) and fan speeds."""
    result = {"cpu_temp": 0.0, "gpu_temp": 0.0, "battery_temp": 0.0, "fan_speed": 0, "fan_count": 0, "fans": []}
    try:
        smc = SMC()
    except OSError:
        return result
    try:
        state = load_state("smc-keys.json")
        if state.get("os") != platform_version() or state.get("v") != SMC_CACHE_VERSION or "keys" not in state:
            all_keys = smc.keys()
            state = {
                "os": platform_version(),
                "v": SMC_CACHE_VERSION,
                "keys": [k for k in all_keys if k.startswith(CPU_TEMP_PREFIXES + GPU_TEMP_PREFIXES) or (k.startswith(BATTERY_TEMP_PREFIXES) and k.endswith("T"))],
            }
            save_state("smc-keys.json", state)

        def avg(prefixes):
            vals = []
            for k in state["keys"]:
                if k.startswith(prefixes):
                    v = smc.read(k)
                    if isinstance(v, float) and 5 < v < 130:
                        vals.append(v)
            return sum(vals) / len(vals) if vals else 0.0

        result["cpu_temp"] = avg(CPU_TEMP_PREFIXES)
        result["gpu_temp"] = avg(GPU_TEMP_PREFIXES)
        result["battery_temp"] = avg(BATTERY_TEMP_PREFIXES)
        count = int(smc.read("FNum") or 0)
        fans = []
        for i in range(count):
            rpm = smc.read("F{}Ac".format(i))
            if rpm is not None and rpm >= 0:
                fans.append(int(round(rpm)))
        result["fan_count"] = count
        result["fans"] = fans
        result["fan_speed"] = int(sum(fans) / len(fans)) if fans else 0
    finally:
        smc.close()
    return result


def platform_version():
    return os.uname().version


STATUS_SYSCTLS = [
    "hw.memsize", "vm.swapusage", "kern.boottime", "kern.memorystatus_vm_pressure_level",
    "machdep.cpu.brand_string", "hw.model", "hw.physicalcpu", "hw.logicalcpu",
    "hw.perflevel0.physicalcpu", "hw.perflevel1.physicalcpu", "hw.perflevel0.name", "hw.perflevel1.name",
]


def status():
    """System status. Rates (CPU, disk, network) are measured against the
    previous call's counters, so repeated calls cost no extra sampling time."""
    now = time.time()
    last = load_state("status-last.json")
    if last.get("data") and 0 <= now - last.get("time", 0) < 1.0:
        return last["data"]  # sampled a moment ago (typing, or the menu bar): reuse it
    prev = load_state("status-sample.json")
    ticks = cpu_ticks()
    if not prev.get("ticks") or now - prev.get("time", 0) > 60 or len(prev["ticks"]) != len(ticks):
        # No recent sample: take a baseline, wait a moment, then measure against it.
        prev = {"time": time.time(), "ticks": ticks, "disk": disk_io_bytes(), "net": network_bytes()}
        time.sleep(0.5)
        ticks = cpu_ticks()
        now = time.time()
    usage, per_core = cpu_usage(prev["ticks"], ticks)
    prefetch([
        ["/usr/sbin/sysctl"] + STATUS_SYSCTLS, ["/usr/bin/vm_stat"], ["/sbin/mount"], ["/bin/df", "-kP"],
        ["/usr/bin/hdiutil", "info", "-plist"], ["/usr/sbin/ioreg", "-c", "IOBlockStorageDriver", "-r", "-a"],
        ["/usr/sbin/netstat", "-ibn"], ["/sbin/ifconfig"], ["/usr/sbin/scutil", "--proxy"],
        ["/usr/sbin/ioreg", "-rn", "AppleSmartBattery", "-a"], ["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator", "-a"],
        ["/bin/ps", "-Aceo", "pcpu=,pmem=,comm=", "-r"], ["/usr/sbin/scutil", "--get", "ComputerName"],
    ])
    disk_io = disk_io_bytes()
    net = network_bytes()
    save_state("status-sample.json", {"time": now, "ticks": ticks, "disk": disk_io, "net": net})
    dt = max(0.001, now - prev.get("time", now - 1))

    sysctls = sysctl_values(STATUS_SYSCTLS)
    hardware = hardware_info(sysctls)
    load1, load5, load15 = os.getloadavg()
    boot = re.search(r"sec = (\d+)", sysctls.get("kern.boottime", ""))
    mem = memory_info(sysctls)
    disks = disk_info()
    batteries, power = battery_info()

    prev_disk = prev.get("disk") or disk_io
    prev_net = prev.get("net") or {}
    ips = interface_ips()
    network = []
    for name, ip in sorted(ips.items()):
        rx, tx = net.get(name, (0, 0))
        prx, ptx = prev_net.get(name, (rx, tx))
        network.append({"name": name, "ip": ip, "rx_rate_mbs": max(0, rx - prx) / dt / 1e6, "tx_rate_mbs": max(0, tx - ptx) / dt / 1e6})

    p_cores = int(sysctls.get("hw.perflevel0.physicalcpu", "0") or 0)
    e_cores = int(sysctls.get("hw.perflevel1.physicalcpu", "0") or 0)
    core_names = [sysctls.get("hw.perflevel0.name") or "P", sysctls.get("hw.perflevel1.name") or "E"]
    thermal = dict(power)
    thermal.update(sensors())
    uptime_days = (now - int(boot.group(1))) / 86400 if boot else 0
    score, message = health_score(usage, mem, disks, batteries, thermal["cpu_temp"], uptime_days)
    busiest, proc_count = top_processes()
    result = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": sh(["/usr/sbin/scutil", "--get", "ComputerName"]).strip(),
        "uptime": format_uptime(now - int(boot.group(1))) if boot else "",
        "procs": proc_count,
        "hardware": hardware,
        "health_score": score,
        "health_score_msg": message,
        "cpu": {
            "usage": usage,
            "per_core": per_core,
            "load1": load1, "load5": load5, "load15": load15,
            "core_count": int(sysctls.get("hw.physicalcpu", "0") or 0),
            "logical_cpu": int(sysctls.get("hw.logicalcpu", "0") or 0),
            "p_core_count": p_cores,
            "e_core_count": e_cores,
            "core_types": core_names,
        },
        "gpu": gpu_info(),
        "memory": mem,
        "disks": disks,
        "disk_io": {
            "read_rate": max(0, disk_io[0] - prev_disk[0]) / dt / 1e6,
            "write_rate": max(0, disk_io[1] - prev_disk[1]) / dt / 1e6,
        },
        "network": network,
        "proxy": proxy_info(),
        "batteries": batteries,
        "thermal": thermal,
        "top_processes": busiest,
    }
    try:
        import updates
        result["app_updates"] = len(updates.visible_updates())
    except Exception:  # noqa: BLE001
        result["app_updates"] = 0
    save_state("status-last.json", {"time": now, "data": result})
    return result


# ---------------------------------------------------------------------------
# Clean: caches, logs, browser and developer caches, old temp files
# ---------------------------------------------------------------------------

MIN_CLEAN_SIZE = 512 * 1024
TEMP_MIN_AGE_DAYS = 3
SYSTEM_REPORT_MIN_AGE_DAYS = 7

# Developer caches: (path under ~, label, markers that mean "the tool is running",
# note). A marker is matched against running processes' command lines.
DEV_ITEMS = [
    ("Library/Caches/Homebrew", "Homebrew downloads", ["/homebrew/library/homebrew/", "/bin/brew "], None),
    ("Library/Caches/pip", "pip cache", ["/pip ", "-m pip", "/pip3 "], None),
    ("Library/Caches/pypoetry/cache", "Poetry cache", ["poetry"], None),        # not pypoetry/virtualenvs!
    ("Library/Caches/pypoetry/artifacts", "Poetry downloads", ["poetry"], None),
    ("Library/Caches/Yarn", "Yarn cache", ["/yarn", "yarn.js"], None),
    ("Library/Caches/pnpm", "pnpm cache", ["pnpm"], None),
    ("Library/Caches/CocoaPods", "CocoaPods cache", ["/pod ", "cocoapods"], None),
    ("Library/Caches/go-build", "Go build cache", ["/go/bin/go ", "/bin/go build", "/bin/go test", "gopls"], None),
    ("Library/Caches/node-gyp", "node-gyp headers", ["node-gyp"], None),
    ("Library/Caches/typescript", "TypeScript cache", ["tsserver"], None),
    ("Library/Caches/com.apple.dt.Xcode", "Xcode cache", ["/xcode.app/"], None),
    ("Library/Caches/deno", "Deno cache", ["/deno "], None),
    ("Library/Caches/bazel", "Bazel cache", ["bazel"], None),
    ("Library/Developer/Xcode/DerivedData", "Xcode DerivedData", ["/xcode.app/", "xcodebuild"], None),
    ("Library/Developer/CoreSimulator/Caches", "Simulator caches", ["/simulator.app/", "coresimulator"], None),
    (".npm/_cacache", "npm cache", ["/npm ", "npm-cli.js", "/npx "], None),
    (".gradle/caches", "Gradle caches", ["gradledaemon", "gradlew", "/gradle "], "needs a download for the next build"),
    (".cargo/registry/cache", "Cargo download cache", ["/cargo "], None),
    (".cache/pip", "pip cache", ["/pip ", "-m pip"], None),
    (".cache/yarn", "Yarn cache", ["/yarn"], None),
    (".cache/go-build", "Go build cache", ["/bin/go "], None),
    (".m2/repository/.cache", "Maven cache", ["/mvn ", "maven"], None),
]
# Large and re-downloadable, but you might want them: listed, never in "Clean all".
OPTIONAL_DEV_ITEMS = [
    ("Library/Developer/Xcode/iOS DeviceSupport", "iOS device support files", ["/xcode.app/"], "rebuilt only when the device is connected again"),
    ("Library/Developer/Xcode/watchOS DeviceSupport", "watchOS device support files", ["/xcode.app/"], "rebuilt only when the device is connected again"),
    ("Library/Developer/Xcode/tvOS DeviceSupport", "tvOS device support files", ["/xcode.app/"], "rebuilt only when the device is connected again"),
    ("Library/Caches/ms-playwright", "Playwright browsers", ["playwright"], "re-downloaded on the next test run"),
]
# App caches that can hold things you'd miss: listed as optional, with why.
OPTIONAL_CACHES = {
    "com.spotify.client": "may include songs downloaded for offline listening",
}
JETBRAINS_APPS = ("IntelliJ", "PyCharm", "WebStorm", "GoLand", "CLion", "Rider", "PhpStorm", "RubyMine",
                  "DataGrip", "DataSpell", "RustRover", "Aqua", "Fleet", "Android Studio", "JetBrains")
# Everything in ~/Library/Caches handled by the developer section
DEV_CACHE_NAMES = {"Homebrew", "pip", "pypoetry", "Yarn", "pnpm", "CocoaPods", "go-build", "JetBrains", "node-gyp",
                   "ms-playwright", "typescript", "com.apple.dt.Xcode", "deno", "bazel"}

# ~/Library/Caches entries of browsers: name -> (label, app-name prefix of the browser)
BROWSER_CACHES = {
    "Google": ("Google Chrome cache", "Google Chrome"),
    "com.google.Chrome": ("Google Chrome cache", "Google Chrome"),
    "BraveSoftware": ("Brave cache", "Brave"),            # shared by Brave, Brave Origin, Beta…
    "Microsoft Edge": ("Microsoft Edge cache", "Microsoft Edge"),
    "com.microsoft.edgemac": ("Microsoft Edge cache", "Microsoft Edge"),
    "Arc": ("Arc cache", "Arc"),
    "company.thebrowser.Browser": ("Arc cache", "Arc"),
    "Firefox": ("Firefox cache", "Firefox"),
    "Mozilla": ("Firefox cache", "Firefox"),
    "com.operasoftware.Opera": ("Opera cache", "Opera"),
    "com.vivaldi.Vivaldi": ("Vivaldi cache", "Vivaldi"),
    "Vivaldi": ("Vivaldi cache", "Vivaldi"),
    "Chromium": ("Chromium cache", "Chromium"),
}
BROWSER_BUNDLE_PREFIXES = [  # most specific first
    ("com.google.Chrome.canary", "Chrome Canary cache", "Google Chrome Canary"),
    ("com.google.Chrome.beta", "Chrome Beta cache", "Google Chrome Beta"),
    ("com.google.Chrome.dev", "Chrome Dev cache", "Google Chrome Dev"),
    ("company.thebrowser.dia", "Dia cache", "Dia"),
    ("com.brave.Browser.beta", "Brave Beta cache", "Brave Browser Beta"),
    ("com.brave.Browser.nightly", "Brave Nightly cache", "Brave Browser Nightly"),
    ("com.brave.Browser.origin", "Brave Origin cache", "Brave Origin"),
    ("com.microsoft.edgemac.Beta", "Edge Beta cache", "Microsoft Edge Beta"),
    ("com.microsoft.edgemac.Dev", "Edge Dev cache", "Microsoft Edge Dev"),
    ("com.google.Chrome", "Google Chrome cache", "Google Chrome"),
    ("com.brave.Browser", "Brave cache", "Brave Browser"),
    ("com.microsoft.edgemac", "Microsoft Edge cache", "Microsoft Edge"),
    ("company.thebrowser.Browser", "Arc cache", "Arc"),
    ("org.mozilla.firefox", "Firefox cache", "Firefox"),
    ("com.operasoftware", "Opera cache", "Opera"),
    ("com.vivaldi", "Vivaldi cache", "Vivaldi"),
    ("org.chromium", "Chromium cache", "Chromium"),
]


def browser_for(name):
    if name in BROWSER_CACHES:
        return BROWSER_CACHES[name]
    for prefix, label, app_prefix in BROWSER_BUNDLE_PREFIXES:
        if name == prefix or name.startswith(prefix + "."):
            return label, app_prefix
    return None


# Chromium-family profile roots under ~/Library/Application Support
CHROMIUM_PROFILES = [
    ("Google/Chrome", "Google Chrome", "Google Chrome"),
    ("BraveSoftware/Brave-Browser", "Brave", "Brave"),
    ("Microsoft Edge", "Microsoft Edge", "Microsoft Edge"),
    ("Arc/User Data", "Arc", "Arc"),
    ("Vivaldi", "Vivaldi", "Vivaldi"),
    ("Chromium", "Chromium", "Chromium"),
]
# Rebuilt automatically. (Service Worker/CacheStorage is left alone: it's offline
# data for web apps, not a disposable cache.)
CHROMIUM_CACHE_DIRS = ["Code Cache", "GPUCache", "Service Worker/ScriptCache", "DawnGraphiteCache", "DawnWebGPUCache"]
# Caches that are protected, shared with the system, or not worth touching.
SKIP_CACHES = {"CloudKit", "FamilyCircle", "GeoServices", "PassKit", "familycircled", "Metadata", "TemporaryItems", "SentryCrash"}


class Activity:
    """What's running and what's open right now, to keep Clean away from files in use."""

    def __init__(self, with_open_files=True):
        self.executables = running_executables()
        # Shell wrappers ("zsh -c …") carry arbitrary text in their arguments; the
        # tools they start show up as their own processes, so skip the wrappers.
        self.args = [l.lower() for l in sh(["/bin/ps", "-Axo", "args="]).splitlines()
                     if not re.match(r"^\S*(?:/|^)(?:ba|z|da|k|c|tc|fi)?sh -\w*c ", l.strip())]
        self.app_names = set()
        for e in self.executables:
            m = re.search(r"([^/]+)\.app/", e)
            if m:
                self.app_names.add(m.group(1))
        self.norm_paths = [_normalize(e) for e in self.executables]
        self.open_files = open_files() if with_open_files else []

    def app_running(self, app_prefix):
        return any(n.startswith(app_prefix) for n in self.app_names)

    def tool_running(self, markers):
        return any(m in line for line in self.args for m in markers)

    def owner_running(self, cache_name, app):
        """The app that owns a cache is running: its bundle, or a helper process
        from anywhere (e.g. /Library/Application Support/AdGuard Software/…)."""
        if app and app_is_running(app["path"], self.executables):
            return True
        parts = [p for p in re.split(r"[.]", cache_name) if p]
        tail = _normalize(parts[-1]) if parts else ""
        if len(tail) >= 5 and tail not in GENERIC_NAMES and any(tail in p for p in self.norm_paths):
            return True
        return False

    def in_use(self, paths):
        """Some process of yours has a file open inside one of these paths."""
        roots = [p.rstrip("/") + "/" for p in paths]
        for f in self.open_files:
            for root in roots:
                if f.startswith(root) or f + "/" == root:
                    return True
        return False


def open_files():
    """Paths currently open by your processes (lsof). Takes a few seconds."""
    out = sh(["/usr/sbin/lsof", "-Fn", "-u", str(os.getuid()), "-w"], timeout=30)
    return [l[1:] for l in out.splitlines() if l.startswith("n/")]


CLEAN_IGNORE = "clean-ignore.json"


def clean_ignored():
    return set(load_state(CLEAN_IGNORE).get("paths", []))


def _item(section, description, paths, admin=False, optional=False, note=None, guard=None):
    """guard: what to re-check just before cleaning ({"app": path, "markers": [...], "name": cache name})."""
    if paths and paths[0] in _IGNORED:
        return None
    size = sum(dir_size(p) for p in paths)
    if size < MIN_CLEAN_SIZE:
        return None
    it = {"type": "item", "section": section, "description": description, "paths": paths, "size": size}
    if admin:
        it["admin"] = True
    if optional:
        it["optional"] = True
    if note:
        it["note"] = note
    if guard:
        it["guard"] = guard
    return it


_IGNORED = set()


def item_busy(item, activity):
    """Re-check, right before cleaning, that nothing is using a scanned item."""
    guard = item.get("guard") or {}
    if guard.get("app_prefix") and activity.app_running(guard["app_prefix"]):
        return True
    if guard.get("markers") and activity.tool_running(guard["markers"]):
        return True
    if guard.get("name") is not None:
        app = {"path": guard["app"]} if guard.get("app") else None
        if activity.owner_running(guard["name"], app):
            return True
    return activity.in_use(item["paths"])


def _newest_mtime(path, limit=2000):
    """Newest modification time inside a tree (a folder's own mtime ignores edits
    to files inside it), plus whether it holds a socket (a live process's)."""
    import stat as statmod
    newest, seen, stack = 0, 0, [path]
    while stack and seen < limit:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        seen += 1
        newest = max(newest, st.st_mtime)
        if statmod.S_ISSOCK(st.st_mode):
            return time.time(), True
        if statmod.S_ISDIR(st.st_mode):
            stack.extend(os.path.join(p, n) for n in _listdir(p))
    if stack:
        return time.time(), False  # too big to check fully: treat as recent
    return newest, False


def clean_scan():
    """Yield cleanable items grouped by section. Anything in use is skipped:
    caches of running apps and their helpers, developer caches while the tool
    runs, and any folder where one of your processes has a file open."""
    global _IGNORED
    _IGNORED = clean_ignored()
    activity = Activity()
    apps = installed_apps_by_bundle_id()
    skipped = []
    caches = os.path.join(HOME, "Library", "Caches")
    entries = sorted(_listdir(caches))

    def busy(paths, label):
        if activity.in_use(paths):
            skipped.append(label)
            return True
        return False

    # Browsers first, so the user caches section doesn't claim them.
    browser_items = {}
    for name in entries:
        b = browser_for(name)
        if not b:
            continue
        label, app_prefix = b
        if activity.app_running(app_prefix):
            skipped.append(label.replace(" cache", ""))
        else:
            browser_items.setdefault((label, app_prefix), []).append(os.path.join(caches, name))
    support = os.path.join(HOME, "Library", "Application Support")
    for rel, label, app_prefix in CHROMIUM_PROFILES:
        root = os.path.join(support, rel)
        if activity.app_running(app_prefix) or not os.path.isdir(root):
            continue
        for profile in _listdir(root):
            if profile != "Default" and not profile.startswith("Profile "):
                continue
            for sub in CHROMIUM_CACHE_DIRS:
                p = os.path.join(root, profile, sub)
                if os.path.isdir(p):
                    browser_items.setdefault((label + " cache", app_prefix), []).append(p)
    yield {"type": "section", "name": "Browsers"}
    for (label, app_prefix), paths in browser_items.items():
        if busy(paths, label.replace(" cache", "")):
            continue
        it = _item("Browsers", label, paths, guard={"app_prefix": app_prefix})
        if it:
            yield it

    yield {"type": "section", "name": "User app caches"}
    for name in entries:
        if browser_for(name) or name in DEV_CACHE_NAMES or name in SKIP_CACHES or name.startswith("com.apple.") or name.startswith("."):
            continue
        app = owning_app(name, apps)
        label_name = app["name"] if app else name
        if activity.owner_running(name, app):
            skipped.append(label_name)
            continue
        path = os.path.join(caches, name)
        if busy([path], label_name):
            continue
        note = OPTIONAL_CACHES.get(name)
        it = _item("Optional" if note else "User app caches", label_name + " cache" if app else name, [path],
                   optional=bool(note), note=note, guard={"name": name, "app": app["path"] if app else None})
        if it:
            yield it

    yield {"type": "section", "name": "Developer tools"}
    dev_items = [(rel, label, markers, note, False) for rel, label, markers, note in DEV_ITEMS]
    dev_items += [(rel, label, markers, note, True) for rel, label, markers, note in OPTIONAL_DEV_ITEMS]
    for rel, label, markers, note, optional in dev_items:
        p = os.path.join(HOME, rel)
        if not os.path.isdir(p):
            continue
        if activity.tool_running(markers):
            skipped.append(label)
            continue
        if busy([p], label):
            continue
        it = _item("Optional" if optional else "Developer tools", label, [p], optional=optional, note=note, guard={"markers": markers})
        if it:
            yield it
    # JetBrains: caches and indexes, never LocalHistory (your edit history)
    jb = os.path.join(caches, "JetBrains")
    if os.path.isdir(jb):
        if any(activity.app_running(n) for n in JETBRAINS_APPS) or activity.tool_running(["/jetbrains/"]):
            skipped.append("JetBrains IDEs")
        else:
            paths = [os.path.join(jb, ide, sub) for ide in _listdir(jb) if os.path.isdir(os.path.join(jb, ide))
                     for sub in _listdir(os.path.join(jb, ide)) if sub != "LocalHistory"]
            if paths and not busy(paths, "JetBrains IDEs"):
                it = _item("Developer tools", "JetBrains IDE caches", paths, guard={"markers": ["/jetbrains/"]})
                if it:
                    yield it

    yield {"type": "section", "name": "Logs"}
    logs = os.path.join(HOME, "Library", "Logs")
    for name in sorted(_listdir(logs)):
        app = owning_app(name, apps)
        label_name = app["name"] if app else re.sub(r"\.log$", "", name)
        if activity.owner_running(re.sub(r"\.log$", "", name), app):
            continue  # its app is writing to it right now
        path = os.path.join(logs, name)
        if activity.in_use([path]):
            continue
        it = _item("Logs", label_name + " logs", [path], guard={"name": re.sub(r"\.log$", "", name), "app": app["path"] if app else None})
        if it:
            yield it

    yield {"type": "section", "name": "Temporary files"}
    tmp = os.environ.get("TMPDIR", "")
    if tmp and os.path.isdir(tmp):
        cutoff = time.time() - TEMP_MIN_AGE_DAYS * 86400
        old = []
        for name in _listdir(tmp):
            # System, locks and live single-instance sockets stay
            if name.startswith((".", "com.apple.")) or name in ("TemporaryItems",) or "Singleton" in name:
                continue
            path = os.path.join(tmp, name)
            newest, has_socket = _newest_mtime(path)
            if not has_socket and newest < cutoff:
                old.append(path)
        old = [p for p in old if not activity.in_use([p])]
        if old:
            it = _item("Temporary files", "Temporary files older than {} days".format(TEMP_MIN_AGE_DAYS), old)
            if it:
                yield it

    # System-wide caches and crash reports: owned by root, so moving them to the
    # Trash asks for a password once.
    yield {"type": "section", "name": "System (needs password)"}
    for name in sorted(_listdir("/Library/Caches")):
        if name.startswith("com.apple.") or name.startswith(".") or name in SKIP_CACHES:
            continue
        app = owning_app(name, apps)
        if activity.owner_running(name, app):
            skipped.append(app["name"] if app else name)
            continue
        it = _item("System (needs password)", (app["name"] + " system cache") if app else name, [os.path.join("/Library/Caches", name)],
                   admin=True, guard={"name": name, "app": app["path"] if app else None})
        if it:
            yield it
    reports = "/Library/Logs/DiagnosticReports"
    report_cutoff = time.time() - SYSTEM_REPORT_MIN_AGE_DAYS * 86400
    old_reports = []
    for n in _listdir(reports):
        path = os.path.join(reports, n)
        try:
            if not n.startswith(".") and os.lstat(path).st_mtime < report_cutoff:
                old_reports.append(path)
        except OSError:
            pass
    if old_reports:
        it = _item("System (needs password)", "System crash reports older than {} days".format(SYSTEM_REPORT_MIN_AGE_DAYS), old_reports, admin=True)
        if it:
            yield it

    # Large but deliberate: listed, never included in "Clean all".
    yield {"type": "section", "name": "Optional"}
    for folder in ("iPhone Software Updates", "iPad Software Updates", "iPod Software Updates"):
        d = os.path.join(HOME, "Library", "iTunes", folder)
        files = [os.path.join(d, n) for n in _listdir(d) if n.endswith(".ipsw")]
        if files:
            it = _item("Optional", folder.replace("Software Updates", "software update files"), files, optional=True)
            if it:
                yield it
    backups = os.path.join(HOME, "Library", "Application Support", "MobileSync", "Backup")
    for name in sorted(_listdir(backups)):
        path = os.path.join(backups, name)
        device = read_plist_key(os.path.join(path, "Info.plist"), "Device Name") or "Device"
        try:
            date = time.strftime("%b %d, %Y", time.localtime(os.stat(path).st_mtime))
        except OSError:
            date = ""
        it = _item("Optional", "{} backup ({})".format(device, date), [path], optional=True, note="your only copy if you don't use iCloud Backup")
        if it:
            yield it
    archives = os.path.join(HOME, "Library", "Developer", "Xcode", "Archives")
    if os.path.isdir(archives):
        it = _item("Optional", "Xcode archives", [os.path.join(archives, n) for n in _listdir(archives) if not n.startswith(".")] or [archives],
                   optional=True, note="needed to symbolicate crash reports of released builds")
        if it:
            yield it

    if skipped:
        names = sorted(set(skipped))
        yield {"type": "note", "message": "In use, so skipped: " + ", ".join(names), "count": len(names)}


# ---------------------------------------------------------------------------
# Purge: build artifacts in project folders
# ---------------------------------------------------------------------------

PROJECT_ROOT_CANDIDATES = [
    "Projects", "projects", "Developer", "Code", "code", "dev", "Dev", "src", "work", "Work", "workspace",
    "Workspace", "repos", "Repos", "GitHub", "git", "Sites", "Documents", "Desktop",
]
PURGE_MAX_DEPTH = 6
MIN_PURGE_SIZE = 1024 * 1024
JS_ARTIFACTS = {".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".angular", ".output", "dist"}


def project_roots():
    configured = os.environ.get("project_dirs", "").strip()
    if configured:
        roots = [os.path.expanduser(p.strip()) for p in configured.split(",") if p.strip()]
    else:
        roots = [os.path.join(HOME, n) for n in PROJECT_ROOT_CANDIDATES]
    seen = []
    for r in roots:
        real = os.path.realpath(r)
        if os.path.isdir(real) and real not in seen:
            seen.append(real)
    return seen


def artifact_kind(parent, name, path):
    """Return the artifact type if `path` is a regenerable build folder."""
    has = lambda f: os.path.exists(os.path.join(parent, f))  # noqa: E731
    if name == "node_modules":
        return name
    if name in JS_ARTIFACTS and has("package.json"):
        return name
    if name == "build" and (has("package.json") or has("build.gradle") or has("build.gradle.kts")):
        return name
    if name == "target" and (has("Cargo.toml") or has("pom.xml")):
        return name
    if name in ("venv", ".venv", "env") and os.path.exists(os.path.join(path, "pyvenv.cfg")):
        return name
    if name == "Pods" and has("Podfile"):
        return name
    if name == ".gradle" and (has("build.gradle") or has("build.gradle.kts") or has("settings.gradle")):
        return name
    return None


ARTIFACT_NAMES = {"node_modules", "build", "target", "venv", ".venv", "env", "Pods", ".gradle"} | JS_ARTIFACTS


def project_mtime(project, entries=None):
    """When the project was last worked on: the newest of its top-level files and
    folders (build folders excluded) and git's index, which changes on every commit."""
    newest = None
    try:
        entries = entries if entries is not None else list(os.scandir(project))
    except OSError:
        return None
    candidates = [e for e in entries if e.name not in ARTIFACT_NAMES]
    for e in candidates:
        try:
            t = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        newest = t if newest is None or t > newest else newest
    try:
        t = os.stat(os.path.join(project, ".git", "index")).st_mtime
        newest = t if newest is None or t > newest else newest
    except OSError:
        pass
    if newest is None:  # nothing but build folders: fall back to the folder itself
        try:
            newest = os.stat(project).st_mtime
        except OSError:
            return None
    return newest


def purge_scan(min_days=None):
    if min_days is None:
        try:
            min_days = int(os.environ.get("purge_min_days", "7") or 7)
        except ValueError:
            min_days = 7
    now = time.time()
    seen = set()
    for root in project_roots():
        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                path = entry.path
                kind = artifact_kind(current, entry.name, path)
                if kind:
                    if path in seen:
                        continue
                    seen.add(path)
                    mtime = project_mtime(current, entries)
                    if mtime is None:
                        continue
                    days = int((now - mtime) // 86400)
                    if days < min_days:
                        continue
                    size = dir_size(path)
                    if size >= MIN_PURGE_SIZE:
                        yield {"type": "item", "path": path, "project": os.path.basename(current), "artifact": kind, "size": size, "days": days}
                    continue
                if entry.name.startswith(".") or entry.name in ("Library", "Applications") or depth >= PURGE_MAX_DEPTH:
                    continue
                if entry.name.endswith((".app", ".photoslibrary", ".musiclibrary", ".xcarchive")):
                    continue
                stack.append((path, depth + 1))


# ---------------------------------------------------------------------------
# Analyze: sizes of a folder's children
# ---------------------------------------------------------------------------

ANALYZE_SKIP_AT_ROOT = {"/Volumes", "/dev", "/System/Volumes", "/net", "/home", "/cores", "/private/var/vm"}


def analyze(path):
    path = os.path.abspath(os.path.expanduser(path))
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        yield {"type": "error", "message": str(e)}
        return
    yield {"type": "start", "path": path, "count": len(entries)}
    dirs = []
    for entry in entries:
        if entry.path in ANALYZE_SKIP_AT_ROOT:
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if entry.is_dir(follow_symlinks=False):
            dirs.append(entry)
        else:
            yield {"type": "entry", "name": entry.name, "path": entry.path, "size": st.st_blocks * 512, "is_dir": False}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(dir_size, e.path): e for e in dirs}
        for fut in as_completed(futures):
            e = futures[fut]
            yield {"type": "entry", "name": e.name, "path": e.path, "size": fut.result(), "is_dir": True}
    yield {"type": "done"}


# ---------------------------------------------------------------------------
# Large files (Spotlight) and Trash size
# ---------------------------------------------------------------------------


def large_files(min_bytes=500 * 1024 * 1024, root=HOME, limit=300):
    """Files bigger than min_bytes under root, biggest first, with when each was
    last opened. Cached for a minute, since Alfred asks on every keystroke."""
    return cached("large-{}".format(int(min_bytes)), 60, lambda: _large_files(min_bytes, root, limit))


def _large_files(min_bytes, root, limit):
    out = sh(["/usr/bin/mdfind", "-onlyin", root, "kMDItemFSSize > {}".format(int(min_bytes))], timeout=30)
    files = []
    for path in out.splitlines():
        path = path.strip()
        if not path or "/.Trash/" in path:
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not os.path.isfile(path) or os.path.islink(path):
            continue  # bundles like .photoslibrary also match; Analyze handles folders
        files.append({"name": os.path.basename(path), "path": path, "size": st.st_size, "mtime": st.st_mtime, "atime": st.st_atime})
    files.sort(key=lambda f: -f["size"])
    files = files[:limit]
    used = last_used_dates([f["path"] for f in files])
    for f in files:
        f["last_used"] = used.get(f["path"])
    return files


def trash_size():
    """Bytes in the user's Trash (cached for a minute), or None when hidden by privacy settings."""
    return cached("trash-size", 60, _trash_size)


def _trash_size():
    trash = os.path.join(HOME, ".Trash")
    try:
        os.listdir(trash)
    except OSError:
        return None
    return dir_size(trash) - (os.lstat(trash).st_blocks * 512)


# ---------------------------------------------------------------------------
# Apps, installers and uninstall residuals
# ---------------------------------------------------------------------------


UNREMOVABLE_APPS = {"Safari.app"}  # part of the sealed system volume


def app_folders():
    dirs = ["/Applications", os.path.join(HOME, "Applications")]
    for base in list(dirs):
        for sub in _listdir(base):
            full = os.path.join(base, sub)
            if not sub.endswith(".app") and not sub.startswith(".") and os.path.isdir(full) and not os.path.islink(full):
                dirs.append(full)
    return dirs


def list_apps():
    apps = []
    for d in app_folders():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".app"):
                continue
            path = os.path.join(d, name)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if name in UNREMOVABLE_APPS:
                continue
            apps.append({"name": name[:-4], "path": path, "mtime": mtime})
    apps.sort(key=lambda a: a["name"].lower())
    return apps


def apps_last_used(paths):
    return last_used_dates(paths)


def last_used_dates(paths):
    """When each file or app was last opened, per Spotlight (None if never recorded)."""
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return {}

    def query(batch):
        out = subprocess.run(["/usr/bin/mdls", "-name", "kMDItemLastUsedDate", "-raw", "-nullMarker", "none"] + batch,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
        return out.split("\0")

    values = query(paths)
    if len(values) != len(paths):
        # mdls stops at the first path it can't read; ask one at a time instead.
        values = [(query([p]) or [""])[0] for p in paths]
    result = {}
    for path, value in zip(paths, values):
        value = value.strip()
        try:
            # mdls prints UTC ("2026-09-26 08:12:34 +0000")
            result[path] = calendar.timegm(time.strptime(value[:19], "%Y-%m-%d %H:%M:%S")) if value and value != "none" else None
        except ValueError:
            result[path] = None
    return result


def app_sizes(out_file):
    apps = list_apps()
    paths = [a["path"] for a in apps]
    with ThreadPoolExecutor(max_workers=8) as pool:
        sizes = dict(zip(paths, pool.map(dir_size, paths)))
    data = {"sizes": sizes, "last_used": apps_last_used(paths)}
    tmp = out_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, out_file)


def quit_app(app_path, bundle_id=None, wait=10):
    """Ask an app to quit (it can still prompt to save). Returns True once it's gone."""
    if bundle_id:
        script = ["on run argv", "tell application id (item 1 of argv) to quit", "end run"]
        arg = bundle_id
    else:
        script = ["on run argv", "tell application (item 1 of argv) to quit", "end run"]
        arg = app_path
    cmd = ["/usr/bin/osascript"]
    for line in script:
        cmd += ["-e", line]
    subprocess.run(cmd + [arg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait
    while time.time() < deadline:
        if not app_is_running(app_path):
            return True
        time.sleep(0.5)
    return not app_is_running(app_path)


INSTALLER_EXTS = (".dmg", ".pkg", ".iso")


def find_installers():
    found = []
    for label in ("Downloads", "Desktop", "Documents"):
        d = os.path.join(HOME, label)
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for entry in entries:
            ext = os.path.splitext(entry)[1].lower()
            if ext not in INSTALLER_EXTS:
                continue
            path = os.path.join(d, entry)
            try:
                size = os.stat(path).st_size
            except OSError:
                continue
            found.append({"name": entry, "path": path, "size": size, "location": label, "ext": ext[1:].upper()})
    found.sort(key=lambda f: -f["size"])
    return found


USER_LIBRARY_DIRS = [
    "Audio/Plug-Ins/Components", "Audio/Plug-Ins/VST", "Audio/Plug-Ins/VST3", "Audio/Plug-Ins/HAL",
    "Screen Savers", "QuickLook", "Spotlight", "Input Methods", "ColorPickers",
    "Application Scripts", "Application Support", "Application Support/CrashReporter", "Caches", "Containers",
    "Cookies", "Group Containers", "HTTPStorages", "Internet Plug-Ins", "LaunchAgents", "Logs",
    "Logs/DiagnosticReports", "Preferences", "Preferences/ByHost", "PreferencePanes", "Saved Application State",
    "Services", "WebKit",
]
SYSTEM_LIBRARY_DIRS = [
    "/Library/Audio/Plug-Ins/Components", "/Library/Audio/Plug-Ins/VST", "/Library/Audio/Plug-Ins/VST3",
    "/Library/Audio/Plug-Ins/HAL", "/Library/Screen Savers", "/Library/QuickLook", "/Library/Spotlight",
    "/Library/Input Methods", "/Library/SystemExtensions/.staged",
    "/Library/Application Support", "/Library/Application Support/CrashReporter", "/Library/Caches",
    "/Library/Extensions", "/Library/Internet Plug-Ins", "/Library/LaunchAgents", "/Library/LaunchDaemons",
    "/Library/Logs", "/Library/Logs/DiagnosticReports", "/Library/Preferences", "/Library/PreferencePanes",
    "/Library/PrivilegedHelperTools",
]
EXTRA_SEARCH_PATHS = [
    os.path.join(HOME, ".config"), "/private/tmp",
]
SKIP_DEEP_SEARCH = {
    "accounts", "addressbook", "appletv", "assistant", "assistants", "audio", "autosave information", "biome",
    "calendars", "callservices", "cloudstorage", "colorpickers", "colors", "compositions", "contacts",
    "containermanager", "daemon containers", "datadeliveryservices", "developer", "donotdisturb", "favorites",
    "finance", "fontcollections", "fonts", "frontboard", "gamekit", "homekit", "identityservices", "input methods",
    "intents", "keychains", "keyboard layouts", "keyboardservices", "languagemodeling", "lockdownmode", "mail",
    "messages", "metadata", "mobile documents", "passes", "photos", "printers", "responsekit", "safari", "sharing",
    "shortcuts", "sounds", "spelling", "spotlight", "suggestions", "translation", "trial", "weather",
}
HELPER_SUFFIX_RE = re.compile(
    r"\.(helper|agent|daemon|service|xpc|launcher|updater|installer|uninstaller|login|extension|plugin|shipit)$", re.I
)
UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$", re.I)


def _normalize(s):
    return re.sub(r"[^a-z0-9]", "", s, flags=re.I).lower()


def read_plist_key(plist_path, key):
    try:
        with open(plist_path, "rb") as f:
            value = plistlib.load(f).get(key)
        return str(value) if value else None
    except Exception:  # noqa: BLE001 — unreadable or odd plists just don't match
        return None


def app_identifiers(app_path):
    plist = app_info_plist(app_path)
    return {
        "bundle_id": read_plist_key(plist, "CFBundleIdentifier"),
        "bundle_name": read_plist_key(plist, "CFBundleName"),
        "display_name": read_plist_key(plist, "CFBundleDisplayName"),
        "executable": read_plist_key(plist, "CFBundleExecutable"),
    }


# Executable names too generic to identify an app on their own.
GENERIC_NAMES = {"electron", "app", "helper", "launcher", "main", "agent", "service", "update", "updater",
                 "client", "desktop", "browser", "player", "run", "start", "setup", "install", "installer"}


# "com.google.Chrome.canary" is a different app from "com.google.Chrome"
CHANNEL_SUFFIXES = {"canary", "beta", "dev", "nightly", "origin", "insiders", "preview", "alpha", "tp", "edge", "next"}


def _search_terms(app_name, ids):
    """Exact names and bundle ids that identify an app's files."""
    names = []
    for n in (app_name, re.sub(r"\s*\d+(\.\d+)*\s*$", "", app_name), ids.get("bundle_name"), ids.get("display_name"), ids.get("executable")):
        norm = _normalize(n or "")
        if len(norm) >= 3 and norm not in GENERIC_NAMES and norm not in names:
            names.append(norm)
    bundle_ids = []
    bid = ids.get("bundle_id")
    if bid and bid.count(".") >= 2:
        bundle_ids.append(bid)
        base = HELPER_SUFFIX_RE.sub("", bid)
        if base != bid and base.count(".") >= 2:
            bundle_ids.append(base)
    # Longer bundle ids of OTHER installed apps (com.brave.Browser.origin for Brave):
    # their files must never be claimed through the shorter prefix.
    others = []
    if bundle_ids:
        prefix = bundle_ids[0].lower() + "."
        others = [b.lower() for b in installed_apps_by_bundle_id() if b.lower().startswith(prefix)]
    return {"names": names, "bundle_ids": bundle_ids, "others": others}


def _matches_app(entry, terms, bundle_id=None, names_ok=True):
    """True when a Library entry belongs to the app. Bundle ids must match on dot
    boundaries (com.foo.App, com.foo.App.helper, TEAMID.com.foo.App) and names
    must match exactly, so com.other.AppExtension never matches "App"."""
    raw = re.sub(r"\.(plist|savedState|binarycookies|log)$", "", entry, flags=re.I)
    low = raw.lower()
    for other in terms.get("others", []):
        if low == other or low.startswith(other + ".") or low.endswith("." + other) or ("." + other + ".") in low:
            return False  # belongs to a longer bundle id: another installed app
    for bid in terms["bundle_ids"]:
        b = bid.lower()
        if low == b or low.endswith("." + b):
            return True
        for sep in (b + ".", "." + b + "."):
            idx = low.find(sep) if sep.startswith(".") else (0 if low.startswith(sep) else -1)
            if idx < 0:
                continue
            rest = low[idx + len(sep):]
            if rest.split(".")[0] in CHANNEL_SUFFIXES:
                continue  # Chrome Canary, Brave Origin… are separate apps
            return True
    return names_ok and _normalize(raw) in terms["names"]


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


# Direct children of ~/Library and /Library are shared system folders ("Developer",
# "Fonts", "Logs"…). An app is only ever matched there by bundle id, never by name.
LIBRARY_ROOTS = {os.path.join(HOME, "Library"), "/Library"}


def _scan_flat(path, terms, bid):
    names_ok = path.rstrip("/") not in LIBRARY_ROOTS
    return [os.path.join(path, e) for e in _listdir(path) if _matches_app(e, terms, bid, names_ok)]


def _scan_deep(path, terms, bid):
    results = _scan_flat(path, terms, bid)
    for entry in _listdir(path):
        if entry.lower() in SKIP_DEEP_SEARCH:
            continue
        sub = os.path.join(path, entry)
        if os.path.isdir(sub) and not os.path.islink(sub):
            results += [os.path.join(sub, s) for s in _listdir(sub) if _matches_app(s, terms, bid)]
    return results


def find_residual_files(app_name, ids, app_path=None):
    """Everything an app leaves outside its bundle: Library folders matched by name
    and bundle id, its declared app-group containers, per-user cache and temp
    folders, crash reports, files its installer package put elsewhere, and its
    Homebrew record. Each entry: {"path", "size", "location"}."""
    residuals = []
    found = set()
    terms = _search_terms(app_name, ids)
    bid = ids.get("bundle_id")

    def add(path, location):
        parent = os.path.dirname(path.rstrip("/"))
        if parent in LIBRARY_ROOTS and not _matches_app(os.path.basename(path), terms, bid, names_ok=False):
            return  # safety net: top-level Library folders only by bundle id
        if path not in found:
            found.add(path)
            residuals.append({"path": path, "size": dir_size(path), "location": location})

    if app_path:
        app_packages(app_path)  # fills _PKG_OWNERS for the receipts lookup below
    for d in USER_LIBRARY_DIRS:
        for m in _scan_flat(os.path.join(HOME, "Library", d), terms, bid):
            add(m, d)
    for d in (os.path.join(HOME, "Library"), "/Library"):
        label = "System Library" if d == "/Library" else "Library"
        for m in _scan_deep(d, terms, bid):
            parent = os.path.basename(os.path.dirname(m))
            add(m, label if parent == os.path.basename(d) else "{}/{}".format(label, parent))
    for d in SYSTEM_LIBRARY_DIRS:
        for m in _scan_flat(d, terms, bid):
            add(m, d.replace("/Library/", "System ", 1))
    for d in EXTRA_SEARCH_PATHS:
        label = d.replace(HOME, "~", 1) if d.startswith(HOME) else d
        for m in _scan_flat(d, terms, bid):
            add(m, label)
    # Installer receipts: package ids that belong to this app (bundle-id match, or
    # the package installed the app itself)
    receipt_ids = set()
    shared_pkgs = packages_with_several_apps()
    for n in _listdir("/private/var/db/receipts"):
        pkg = re.sub(r"\.(bom|plist)$", "", n)
        if pkg in shared_pkgs:
            continue  # the package also installed other apps: keep its receipt
        if _matches_app(n, terms, bid) or pkg in _PKG_OWNERS.get(app_path or "", []):
            receipt_ids.add(os.path.join("/private/var/db/receipts", n))
    for path in sorted(receipt_ids):
        add(path, "Installer receipts")

    if bid:
        parts = bid.split(".")
        vendor = parts[1].lower() if len(parts) >= 3 and len(parts[1]) >= 2 else None
        if vendor:
            for d in (
                os.path.join(HOME, "Library", "Application Support"),
                os.path.join(HOME, "Library", "Caches"),
                os.path.join(HOME, "Library", "Logs"),
                "/Library/Application Support",
                "/Library/Caches",
            ):
                vendor_dir = os.path.join(d, vendor[0].upper() + vendor[1:])
                if os.path.isdir(vendor_dir):
                    label = "System " + os.path.basename(d) if d.startswith("/Library") else os.path.basename(d)
                    for m in _scan_flat(vendor_dir, terms, bid):
                        add(m, "{}/{}".format(label, os.path.basename(vendor_dir)))
        containers = os.path.join(HOME, "Library", "Containers")
        for entry in _listdir(containers):
            if UUID_RE.match(entry):
                meta = os.path.join(containers, entry, ".com.apple.containermanagerd.metadata.plist")
                if read_plist_key(meta, "MCMMetadataIdentifier") == bid:
                    add(os.path.join(containers, entry), "Containers")

    for cask in homebrew_casks(app_name, ids):
        add(cask, "Homebrew")

    if bid:
        # Hidden per-user cache and temp folders (/var/folders/…/C and …/T)
        for kind, label in (("DARWIN_USER_CACHE_DIR", "System cache folder"), ("DARWIN_USER_TEMP_DIR", "System temp folder")):
            base = darwin_dir(kind)
            if base:
                for m in _scan_flat(base, {"names": [], "bundle_ids": terms["bundle_ids"]}, bid):
                    add(m, label)
    if app_path:
        # Shared folders the app declares in its signature (exact names, no guessing).
        # Groups another installed app from the same developer also uses stay put.
        shared = groups_used_by_siblings(app_path, bid)
        for group in app_groups(app_path):
            if group in shared:
                continue
            path = os.path.join(HOME, "Library", "Group Containers", group)
            if os.path.exists(path):
                add(path, "Group Containers")
        # Files its installer package put outside the app
        for path in package_files(app_path):
            add(path, "Installed by package")
    # Crash reports are named "<Executable>_2026-09-26-….ips" or "<Executable>-…"
    exe = ids.get("executable") or app_name
    if exe and len(exe) >= 3 and _normalize(exe) not in GENERIC_NAMES:
        for d, label in ((os.path.join(HOME, "Library", "Logs", "DiagnosticReports"), "Crash reports"),
                         ("/Library/Logs/DiagnosticReports", "System crash reports")):
            for n in _listdir(d):
                if n.startswith((exe + "_", exe + "-", exe + ".")) and n.endswith((".ips", ".crash", ".diag", ".spin", ".hang")):
                    add(os.path.join(d, n), label)

    # A folder and something inside it would otherwise be listed and counted twice.
    paths = sorted(r["path"] for r in residuals)
    nested = set(p for p in paths if any(p != q and p.startswith(q.rstrip("/") + "/") for q in paths))
    residuals = [r for r in residuals if r["path"] not in nested]
    residuals.sort(key=lambda r: -r["size"])
    return residuals


def darwin_dir(name):
    try:
        path = cached("getconf-" + name, 86400, lambda: sh(["/usr/bin/getconf", name]).strip())
    except Exception:  # noqa: BLE001
        return None
    return path.rstrip("/") if path and os.path.isdir(path) else None


def app_entitlements(app_path):
    out = subprocess.run(["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", app_path],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15).stdout
    try:
        return plistlib.loads(out) if out.strip() else {}
    except Exception:  # noqa: BLE001 — unsigned or odd apps have no entitlements
        return {}


def app_groups(app_path):
    """App-group containers the app is entitled to (~/Library/Group Containers/<id>)."""
    groups = signing_info(app_path)["groups"]
    return [g for g in groups if isinstance(g, str) and "." in g and not g.startswith(("com.apple.", "group.com.apple."))]


def signing_info(app_path):
    """{"team": team id or None, "groups": [app groups]} from the code signature."""
    ent = app_entitlements(app_path)
    groups = [g for g in (ent.get("com.apple.security.application-groups") or []) if isinstance(g, str)]
    team = ent.get("com.apple.developer.team-identifier")
    if not team:
        out = subprocess.run(["/usr/bin/codesign", "-dv", app_path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15).stderr.decode("utf-8", "replace")
        m = re.search(r"TeamIdentifier=([A-Z0-9]{10})", out)
        team = m.group(1) if m else None
    return {"team": team, "groups": groups}


def all_signing_info():
    """signing_info for every installed app (Apple's included), cached on disk and
    only recomputed for apps that changed. path -> {"team", "groups"}"""
    cache = load_state("signing.json")
    apps = {a["path"] for a in installed_apps_by_bundle_id().values()}
    out, todo = {}, []
    for path in apps:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        hit = cache.get(path)
        if hit and hit.get("mtime") == mtime:
            out[path] = hit
        else:
            todo.append((path, mtime))
    if todo:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for (path, mtime), info in zip(todo, pool.map(lambda t: signing_info(t[0]), todo)):
                out[path] = dict(info, mtime=mtime)
        save_state("signing.json", out)
    return out


def groups_used_by_siblings(app_path, bundle_id):
    """App groups declared by the developer's OTHER installed apps (same team or
    same bundle-id vendor), so removing one app never takes the others' data."""
    if not bundle_id:
        return set()
    vendor = ".".join(bundle_id.split(".")[:2]).lower()
    info = all_signing_info()
    team = (info.get(app_path) or {}).get("team")
    by_path = {a["path"]: bid for bid, a in installed_apps_by_bundle_id().items()}
    shared = set()
    for path, meta in info.items():
        if path == app_path:
            continue
        same_vendor = by_path.get(path, "").lower().startswith(vendor + ".")
        if same_vendor or (team and meta.get("team") == team):
            shared.update(meta.get("groups") or [])
    return shared


def app_team_id(app_path):
    ent = app_entitlements(app_path)
    team = ent.get("com.apple.developer.team-identifier")
    if team:
        return team
    out = subprocess.run(["/usr/bin/codesign", "-dv", app_path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15).stderr.decode("utf-8", "replace")
    m = re.search(r"TeamIdentifier=([A-Z0-9]{10})", out)
    return m.group(1) if m else None


# Never offer to remove these, whatever a package receipt says it installed.
PACKAGE_PROTECTED = ("/System/", "/bin/", "/sbin/", "/usr/", "/opt/", "/etc/", "/private/etc/",
                     "/Applications/", "/Library/Apple/", "/Library/Frameworks/", "/Library/Fonts/",
                     "/private/var/db/", "/Library/Developer/", "/Library/Python/", "/Library/Java/")
BUNDLE_EXTS = (".app", ".plugin", ".bundle", ".framework", ".kext", ".component", ".vst", ".vst3", ".driver",
               ".prefPane", ".qlgenerator", ".mdimporter", ".saver", ".appex", ".systemextension", ".dext", ".mpkg", ".pkg")


def app_packages(app_path):
    """Installer receipts (pkgutil ids) whose package installed this app."""
    if app_path in _PKG_OWNERS:
        return _PKG_OWNERS[app_path]
    app_path = os.path.normpath(app_path)
    base = os.path.basename(app_path)
    copies = sum(1 for a in list_apps() if os.path.basename(a["path"]) == base)
    owners = []
    for pkg, (location, files) in all_package_files().items():
        for line in files[:400]:
            parts = [x for x in line.split("/") if x]
            if base not in parts[:2]:
                continue
            k = parts.index(base)
            installed_at = os.path.normpath(os.path.join(location, *parts[:k + 1]))
            if installed_at == app_path:
                owners.append(pkg)  # installed exactly here
            elif copies == 1 and not location.startswith(("/Applications", os.path.join(HOME, "Applications"))):
                owners.append(pkg)  # installed via a staging folder (Word's updater clones), and there's only one such app
            break
    _PKG_OWNERS[app_path] = owners
    return owners


def all_package_files():
    """pkgid -> (absolute install location, [file lines]) for third-party packages. Cached 10 min."""
    def compute():
        pkgs = [p for p in sh(["/usr/sbin/pkgutil", "--pkgs"]).split() if not p.startswith("com.apple.")]

        def one(pkg):
            info = sh_plist(["/usr/sbin/pkgutil", "--pkg-info-plist", pkg]) or {}
            location = "/" + str(info.get("install-location", "")).strip("/")
            return pkg, [location, sh(["/usr/sbin/pkgutil", "--files", pkg], timeout=20).splitlines()]
        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(pool.map(one, pkgs))
    return {k: tuple(v) for k, v in cached("package-files", 600, compute).items()}


_PKG_OWNERS = {}


def packages_with_several_apps():
    """Package ids that installed more than one app (e.g. Python's IDLE + Launcher)."""
    shared = set()
    for pkg, (location, files) in all_package_files().items():
        apps = set()
        for line in files:
            parts = [x for x in line.split("/") if x]
            for part in parts[:2]:
                if part.endswith(".app"):
                    apps.add(part)
        if len(apps) > 1:
            shared.add(pkg)
    return shared


def package_files(app_path):
    """Files and bundles the app's installer packages put outside the app itself
    (helper tools, plug-ins, drivers), that still exist and no other package claims."""
    base = os.path.basename(app_path.rstrip("/"))
    units = set()
    mine = app_packages(app_path)
    everything = all_package_files()
    # Every file some OTHER package installed: shared, so never removed with this app.
    claimed = set()
    for pkg, (location, files) in everything.items():
        if pkg not in mine:
            claimed.update(os.path.normpath(os.path.join(location, line)) for line in files)
    for pkg in mine:
        location, files = everything.get(pkg, ("/", []))
        for line in files:
            parts = [x for x in line.split("/") if x]
            if not parts or parts[0] == base or (len(parts) > 1 and parts[0] == "Applications" and parts[1] == base):
                continue
            # Collapse anything inside a bundle to the bundle itself
            for i, part in enumerate(parts):
                if part.endswith(BUNDLE_EXTS):
                    parts = parts[:i + 1]
                    break
            path = os.path.normpath(os.path.join(location, *parts))
            if path.startswith(PACKAGE_PROTECTED) or path.startswith(app_path.rstrip("/") + "/") or path in claimed:
                continue
            if ".app/" in path + "/" and not path.startswith(app_path):
                continue  # inside (or is) another app
            if path.endswith(".app"):
                continue
            if os.path.isdir(path) and not os.path.islink(path) and not path.endswith(BUNDLE_EXTS):
                continue  # plain folders like /Library/Application Support are shared
            if os.path.lexists(path):
                units.add(path)
    return sorted(units)[:200]


def system_extensions(bundle_id, team_id=None):
    """System extensions (network filters, drivers) that belong to the app."""
    found = []
    if not bundle_id:
        return found
    vendor = ".".join(bundle_id.split(".")[:2]).lower()
    for line in sh(["/usr/bin/systemextensionsctl", "list"]).splitlines():
        m = re.match(r"^\s*\*?\s*\*?\s*([A-Z0-9]{10})\s+(\S+)\s+\(([^)]*)\)\s+(.*?)\s+\[(.*)\]\s*$", line)
        if not m:
            continue
        team, ext_id, _, name, state = m.groups()
        if (team_id and team == team_id) or ext_id.lower().startswith(vendor + "."):
            found.append({"id": ext_id, "name": name, "state": state})
    return found


def vendor_uninstallers(app_path, app_name):
    """Uninstaller apps shipped with the app (for software with drivers or extensions)."""
    found = []
    candidates = [os.path.join(app_path, "Contents", "Resources"), os.path.join(app_path, "Contents", "SharedSupport"),
                  os.path.dirname(app_path)]
    first = _normalize(app_name.split()[0]) if app_name.split() else ""
    for d in candidates:
        for n in _listdir(d):
            low = n.lower()
            if n.endswith(".app") and "uninstall" in low and os.path.join(d, n) != app_path:
                if d == os.path.dirname(app_path) and d in ("/Applications", os.path.join(HOME, "Applications")):
                    if first and first not in _normalize(n):
                        continue  # in /Applications, only uninstallers named after this app
                found.append(os.path.join(d, n))
    return found


def remove_from_dock(app_path):
    """Remove the app's Dock icon, if it has one. Returns True if the Dock changed."""
    raw = subprocess.run(["/usr/bin/defaults", "export", "com.apple.dock", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    try:
        dock = plistlib.loads(raw)
    except Exception:  # noqa: BLE001
        return False
    from urllib.parse import unquote
    target = app_path.rstrip("/")
    apps = dock.get("persistent-apps") or []

    def points_here(tile):
        url = (((tile.get("tile-data") or {}).get("file-data") or {}).get("_CFURLString") or "")
        return unquote(url.replace("file://", "")).rstrip("/") == target

    kept = [t for t in apps if not points_here(t)]
    if len(kept) == len(apps):
        return False
    dock["persistent-apps"] = kept
    res = subprocess.run(["/usr/bin/defaults", "import", "com.apple.dock", "-"], input=plistlib.dumps(dock), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode == 0:
        subprocess.run(["/usr/bin/killall", "Dock"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


def force_quit(app_path):
    """SIGKILL every process running from inside the app bundle."""
    root = app_path.rstrip("/") + "/"
    for line in sh(["/bin/ps", "-Axo", "pid=,comm="]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].startswith(root):
            try:
                os.kill(int(parts[0]), 9)
            except OSError:
                pass
    time.sleep(0.5)
    return not app_is_running(app_path)


def needs_root(path):
    """Moving `path` needs admin rights (its folder isn't writable by us)."""
    parent = os.path.dirname(path.rstrip("/"))
    return not os.access(parent, os.W_OK)


def admin_trash(paths, label, batch_id=None, prompt=None, before=(), after=()):
    """Move root-owned paths into the user's Trash with ONE password prompt,
    running `before`/`after` shell commands in the same prompt (unload daemons,
    forget installer receipts). Recorded for Undo. Returns what couldn't move."""
    import shlex
    if not paths and not before and not after:
        return []
    trash = os.path.join(HOME, ".Trash")
    tag = (batch_id or new_batch_id()).replace(".", "")[-8:]
    moves, lines = {}, list(before)
    for i, p in enumerate(paths):
        dest = os.path.join(trash, "{} {}-{}".format(os.path.basename(p.rstrip("/")), tag, i + 1))
        moves[p] = dest
        # Ownership is kept (a restored daemon must stay root-owned to load);
        # never move into an existing folder.
        lines.append("[ ! -e {d} ] && mv {s} {d}".format(s=shlex.quote(p), d=shlex.quote(dest)))
    lines += list(after)
    run_as_admin("; ".join(lines) or "true", prompt or "Burrow needs your password to remove items owned by the system.")
    done = {k: v for k, v in moves.items() if not os.path.lexists(k) and os.path.lexists(v)}
    if done:
        record_trash_batch(label, done, batch_id, root=True)
    return [k for k in moves if os.path.lexists(k)]


CASKROOMS = ["/opt/homebrew/Caskroom", "/usr/local/Caskroom"]


def homebrew_casks(app_name, ids):
    """Caskroom folders for an app installed with `brew install --cask`, so
    Homebrew forgets the app once it's removed."""
    names = {_normalize(app_name), _normalize(re.sub(r"\s*\d+(\.\d+)*\s*$", "", app_name))}
    for k in ("bundle_name", "display_name"):
        if ids.get(k):
            names.add(_normalize(ids[k]))
    names.discard("")
    found = []
    for room in CASKROOMS:
        for token in _listdir(room):
            if token.startswith("."):
                continue
            if _normalize(token) in names:
                found.append(os.path.join(room, token))
                continue
            # The cask's own metadata names the .app it installed.
            meta = os.path.join(room, token, ".metadata")
            if any(("/" + app_name + ".app") in text for text in _cask_texts(meta)):
                found.append(os.path.join(room, token))
    return found


def _cask_texts(meta_dir, limit=6):
    texts = []
    for base, _, files in os.walk(meta_dir):
        for f in files:
            if f.endswith((".rb", ".json")) and len(texts) < limit:
                try:
                    with open(os.path.join(base, f), errors="replace") as fh:
                        texts.append(fh.read(200000))
                except OSError:
                    pass
    return texts


def stop_background_items(app_path, residuals):
    """Unload the app's user LaunchAgents and end helper processes still running
    from the app or its leftover folders, so nothing keeps running from the Trash."""
    uid = os.getuid()
    agent_dirs = (os.path.join(HOME, "Library", "LaunchAgents"), "/Library/LaunchAgents")
    for r in residuals:
        if os.path.dirname(r["path"]) in agent_dirs and r["path"].endswith(".plist"):
            label = read_plist_key(r["path"], "Label")
            if label:
                subprocess.run(["/bin/launchctl", "bootout", "gui/{}/{}".format(uid, label)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    roots = [app_path.rstrip("/") + "/"] + [r["path"].rstrip("/") + "/" for r in residuals if os.path.isdir(r["path"])]
    for line in sh(["/bin/ps", "-Axo", "pid=,comm="]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != os.getpid() and any(parts[1].startswith(root) for root in roots):
            try:
                os.kill(int(parts[0]), 15)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Optimize
# ---------------------------------------------------------------------------

LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

# default: included in "Optimize All". admin: needs the password prompt.
OPTIMIZATIONS = [
    {"id": "dns", "section": "Network", "title": "Flush DNS cache", "detail": "Fixes sites that don't load after DNS changes",
     "cmd": "dscacheutil -flushcache; killall -HUP mDNSResponder", "admin": True, "default": True, "requires": "/usr/bin/dscacheutil"},
    {"id": "memory", "section": "Memory", "title": "Free inactive memory", "detail": "Purges disk caches held in RAM",
     "cmd": "purge", "admin": True, "default": True, "requires": "/usr/sbin/purge"},
    {"id": "periodic", "section": "System", "title": "Run maintenance scripts", "detail": "Runs the daily, weekly and monthly periodic jobs",
     "cmd": "periodic daily weekly monthly", "admin": True, "default": True, "requires": "/usr/sbin/periodic"},
    {"id": "launchservices", "section": "System", "title": "Rebuild Launch Services database", "detail": "Fixes duplicate apps in Open With menus",
     # -f re-registers every app, -gc compacts the database; no reboot needed (unlike -delete)
     "cmd": LSREGISTER + " -gc -r -f -all local,system,user", "admin": False, "default": True, "requires": LSREGISTER},
    {"id": "quicklook", "section": "System", "title": "Reset Quick Look", "detail": "Clears thumbnail and preview caches",
     "cmd": "qlmanage -r cache; qlmanage -r", "admin": False, "default": True, "requires": "/usr/bin/qlmanage"},
    {"id": "fonts", "section": "System", "title": "Clear font caches", "detail": "Fixes garbled or missing fonts",
     # macOS 14+ has no ATS server to restart; clear the databases, then fontd reloads them
     "cmd": "atsutil databases -removeUser && { killall fontd 2>/dev/null; true; }", "admin": False, "default": True, "requires": "/usr/bin/atsutil"},
    {"id": "dock", "section": "Interface", "title": "Restart Dock", "detail": "Refreshes the Dock, Mission Control and Launchpad",
     "cmd": "killall Dock", "admin": False, "default": True, "requires": "/usr/bin/killall"},
    {"id": "finder", "section": "Interface", "title": "Restart Finder", "detail": "Closes open Finder windows",
     "cmd": "killall Finder", "admin": False, "default": False, "requires": "/usr/bin/killall"},
    {"id": "snapshots", "section": "Disk", "title": "Delete local Time Machine snapshots", "detail": "Frees space macOS reserves for hourly snapshots; your backup disk isn't touched",
     "cmd": "tmutil deletelocalsnapshots / || tmutil thinlocalsnapshots / 999999999999 4", "admin": True, "default": False, "requires": "/usr/bin/tmutil"},
    {"id": "simulators", "section": "Developer", "title": "Delete unavailable simulators", "detail": "Removes iOS simulators left over from old Xcode versions",
     "cmd": "xcrun simctl delete unavailable", "admin": False, "default": False, "requires": "/Applications/Xcode.app"},
    {"id": "brew", "section": "Developer", "title": "Clean up Homebrew", "detail": "Removes old versions of installed packages and cached downloads",
     "cmd": "{brew} cleanup --prune=all -s", "admin": False, "default": False, "requires": "{brew}"},
    {"id": "docker", "section": "Developer", "title": "Prune Docker", "detail": "Removes stopped containers, unused networks, dangling images and build cache (Docker must be running)",
     "cmd": "{docker} system prune -f", "admin": False, "default": False, "requires": "{docker}"},
    {"id": "spotlight", "section": "Spotlight", "title": "Rebuild Spotlight index", "detail": "Re-indexes the startup disk; search is incomplete for a while",
     "cmd": "mdutil -E /", "admin": True, "default": False, "requires": "/usr/bin/mdutil"},
]


def local_snapshot_count():
    return cached("snapshot-count", 300, _local_snapshot_count)


def _local_snapshot_count():
    return sum(1 for l in sh(["/usr/bin/tmutil", "listlocalsnapshots", "/"]).splitlines() if "com.apple.TimeMachine" in l)


def find_tool(name):
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin"):
        path = os.path.join(d, name)
        if os.access(path, os.X_OK):
            return path
    return None


def optimizations():
    tools = {"brew": find_tool("brew") or "/nonexistent/brew", "docker": find_tool("docker") or "/nonexistent/docker"}
    tasks = []
    for o in OPTIMIZATIONS:
        if "{brew}" in o["cmd"] or "{docker}" in o["cmd"]:
            fill = lambda text: text.replace("{brew}", tools["brew"]).replace("{docker}", tools["docker"])  # noqa: E731
            o = dict(o, cmd=fill(o["cmd"]), requires=fill(o["requires"]))
        if not os.path.exists(o["requires"]):
            continue
        if o["id"] == "snapshots":
            count = local_snapshot_count()
            if not count:
                continue
            o = dict(o, detail="{} snapshot{} · {}".format(count, "" if count == 1 else "s", o["detail"]))
        tasks.append(o)
    return tasks


def run_optimizations(ids):
    """Run the given optimizations. Admin ones share one password prompt.
    Returns {"ok": [...titles], "failed": [...titles], "cancelled": bool}."""
    tasks = [o for o in optimizations() if o["id"] in ids]
    ok, failed = [], []
    admin = [t for t in tasks if t["admin"]]
    cancelled = False
    if admin:
        script = "; ".join("( {} ) >/dev/null 2>&1 && echo ok:{} || echo fail:{}".format(t["cmd"], t["id"], t["id"]) for t in admin)
        prompt = "Burrow needs your password to: " + ", ".join(t["title"].lower() for t in admin) + "."
        res, out = run_as_admin(script, prompt)
        if res is None:
            cancelled = True
        for t in admin:
            if "ok:" + t["id"] in (out or ""):
                ok.append(t["title"])
            elif not cancelled:
                failed.append(t["title"])
    for t in tasks:
        if t["admin"]:
            continue
        r = subprocess.run(["/bin/sh", "-c", t["cmd"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=TOOL_ENV)
        (ok if r.returncode == 0 else failed).append(t["title"])
    return {"ok": ok, "failed": failed, "cancelled": cancelled}


# ---------------------------------------------------------------------------
# Touch ID for sudo (macOS 14+: /etc/pam.d/sudo_local survives updates)
# ---------------------------------------------------------------------------

SUDO_LOCAL = "/etc/pam.d/sudo_local"
PAM_TID_RE = re.compile(r"^\s*auth\s+sufficient\s+pam_tid\.so", re.M)


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def touchid_status():
    supported = os.path.exists(SUDO_LOCAL) or os.path.exists(SUDO_LOCAL + ".template")
    in_local = bool(PAM_TID_RE.search(_read(SUDO_LOCAL)))
    in_sudo = bool(PAM_TID_RE.search(_read("/etc/pam.d/sudo")))
    return {"supported": supported, "enabled": in_local or in_sudo, "managed": in_local and not in_sudo, "legacy": in_sudo}


def touchid_set(enable):
    if enable:
        script = (
            'f={f}; if [ -f "$f" ] && grep -qE "^[[:space:]]*auth[[:space:]]+sufficient[[:space:]]+pam_tid\\.so" "$f"; then :; '
            'elif [ -f "$f" ] && grep -qE "^#[[:space:]]*auth[[:space:]]+sufficient[[:space:]]+pam_tid\\.so" "$f"; then '
            "sed -i '' -E 's/^#[[:space:]]*(auth[[:space:]]+sufficient[[:space:]]+pam_tid\\.so)/\\1/' \"$f\"; "
            'else echo "auth       sufficient     pam_tid.so" >> "$f"; fi; chmod 444 "$f"'
        ).format(f=SUDO_LOCAL)
        prompt = "Burrow needs your password to turn on Touch ID for sudo."
    else:
        script = "sed -i '' -E 's/^([[:space:]]*auth[[:space:]]+sufficient[[:space:]]+pam_tid\\.so)/#\\1/' {}".format(SUDO_LOCAL)
        prompt = "Burrow needs your password to turn off Touch ID for sudo."
    ok, out = run_as_admin(script, prompt)
    if ok and touchid_status()["enabled"] != enable:
        return False, "the change didn't take effect; check /etc/pam.d/sudo_local"
    return ok, out


# ---------------------------------------------------------------------------
# Leftovers of apps that are no longer installed
# ---------------------------------------------------------------------------

BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9_-]+){2,}$")
ORPHAN_DIRS = [
    ("Application Support", False), ("Caches", False), ("Containers", True), ("Preferences", False),
    ("Saved Application State", False), ("HTTPStorages", False), ("WebKit", False), ("Logs", False), ("Cookies", False),
]
# Vendor prefixes that are part of macOS or shared frameworks, not apps
ORPHAN_SKIP_PREFIXES = ("com.apple.", "group.com.apple.", "systemgroup.", "com.microsoft.autoupdate", "org.sparkle-project",
                        "com.google.keystone", "com.crashlytics", "io.sentry", "com.segment", "com.bugsnag")


ORPHAN_QUIET_DAYS = 14


def _bundle_id_of(name, folder):
    base = re.sub(r"\.(plist|savedState|binarycookies)$", "", name)
    if folder == "Containers" and not BUNDLE_ID_RE.match(base):
        return None
    return base if BUNDLE_ID_RE.match(base) else None


def orphaned_leftovers():
    """Library items named after a bundle id that no installed app, running
    process or startup item uses. Grouped by bundle id, biggest first."""
    apps = installed_apps_by_bundle_id()
    known = [b.lower() for b in apps]
    for e in running_executables():
        m = re.search(r"/([^/]+)\.app/", e)
        if m:
            known.append(m.group(1).lower())
    for item in startup_items():
        known.append(item["label"].lower())

    # Any installed app from the same vendor (com.kaspersky.*) might still use it
    # under a slightly different name, so the whole vendor counts as in use.
    vendors = set(".".join(k.split(".")[:2]) for k in known if k.count(".") >= 2)
    signing = all_signing_info()
    used_groups = set(g.lower() for meta in signing.values() for g in meta.get("groups") or [])
    used_teams = set(meta.get("team") for meta in signing.values() if meta.get("team"))

    def owned(bid):
        b = bid.lower()
        if ".".join(b.split(".")[:2]) in vendors:
            return True
        for k in known:
            # the app itself, one of its helpers/extensions, or a parent id
            if b == k or b.startswith(k + ".") or k.startswith(b + "."):
                return True
        return False

    groups = {}
    lib = os.path.join(HOME, "Library")
    bases = [(os.path.join(lib, folder), folder) for folder, _ in ORPHAN_DIRS]
    bases += [(d, "cache") for d in (darwin_dir("DARWIN_USER_CACHE_DIR"), darwin_dir("DARWIN_USER_TEMP_DIR")) if d]
    for base, folder in bases:
        for name in _listdir(base):
            bid = _bundle_id_of(name, folder)
            if not bid or bid.lower().startswith(ORPHAN_SKIP_PREFIXES) or owned(bid):
                continue
            groups.setdefault(bid, []).append(os.path.join(base, name))
    group_root = os.path.join(lib, "Group Containers")
    for name in _listdir(group_root):
        low = name.lower()
        if low in used_groups or "com.apple." in low or low.startswith(("group.com.apple", "systemgroup.")):
            continue  # an installed app declares this group, or it's macOS's
        m = re.match(r"^([A-Z0-9]{10})\.(.+)$", name)
        if m and m.group(1) in used_teams:
            continue  # same developer team as an installed app
        rest = m.group(2) if m else name
        bid = re.sub(r"^(groups?\.)", "", rest)
        if not BUNDLE_ID_RE.match(bid) or bid.lower().startswith(ORPHAN_SKIP_PREFIXES) or owned(bid):
            continue
        groups.setdefault(bid, []).append(os.path.join(group_root, name))
    result = []
    recent = time.time() - ORPHAN_QUIET_DAYS * 86400
    for bid, paths in groups.items():
        try:
            newest = max(os.lstat(p).st_mtime for p in paths)
        except (OSError, ValueError):
            continue
        if newest > recent:
            continue  # something wrote here lately, so it's probably still in use
        size = sum(dir_size(p) for p in paths)
        result.append({"bundle_id": bid, "paths": sorted(paths), "size": size, "last_modified": newest})
    result.sort(key=lambda g: -g["size"])
    return result


# ---------------------------------------------------------------------------
# Startup items: launch agents and daemons
# ---------------------------------------------------------------------------

LAUNCH_DIRS = [
    (os.path.join(HOME, "Library", "LaunchAgents"), "Login agent", False),
    ("/Library/LaunchAgents", "Agent for all users", True),
    ("/Library/LaunchDaemons", "System daemon", True),
]


def program_is_gone(program):
    """True only when we can see the program's folder and the program isn't in it.
    Unplugged drives and privacy-protected folders count as unknown, not missing."""
    if os.path.exists(program) or program.startswith("/Volumes/"):
        return False
    parent = os.path.dirname(program)
    while parent and parent != "/" and not os.path.exists(parent):
        parent = os.path.dirname(parent)
    try:
        os.listdir(parent or "/")
    except OSError:
        return False
    return True


def startup_items():
    """Third-party launch agents/daemons, with the app they belong to and whether
    their program is missing (left behind by an app that was deleted)."""
    uid = os.getuid()
    disabled = set()
    for line in sh(["/bin/launchctl", "print-disabled", "gui/{}".format(uid)]).splitlines() + sh(["/bin/launchctl", "print-disabled", "system"]).splitlines():
        m = re.match(r'\s*"([^"]+)" => (disabled|true)', line)
        if m:
            disabled.add(m.group(1))
    loaded = set()
    for line in sh(["/bin/launchctl", "list"]).splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            loaded.add(parts[2])
    apps = installed_apps_by_bundle_id()
    items = []
    for folder, kind, system in LAUNCH_DIRS:
        for name in sorted(_listdir(folder)):
            if not name.endswith(".plist") or name.startswith("com.apple."):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, "rb") as f:
                    plist = plistlib.load(f)
            except Exception:  # noqa: BLE001 — unreadable plists are still listed
                plist = {}
            label = str(plist.get("Label") or name[:-6])
            program = plist.get("Program") or (plist.get("ProgramArguments") or [None])[0]
            program = str(program) if program else ""
            missing = bool(program) and program.startswith("/") and program_is_gone(program)
            owner = None
            m = re.search(r"^(.*?\.app)/", program)
            if m:
                owner = os.path.basename(m.group(1))[:-4]
            if not owner:
                app = owning_app(label, apps) or owning_app(name[:-6], apps)
                owner = app["name"] if app else None
            items.append({
                "path": path, "label": label, "kind": kind, "system": system, "program": program,
                "missing": missing, "owner": owner, "disabled": label in disabled,
                "loaded": label in loaded if not system else None,
            })
    return items


def remove_startup_items(entries, batch_id=None):
    """Stop agents/daemons and move their plists to the Trash. Everything that
    needs root (system daemons, files in /Library) shares one password prompt.
    Returns the paths that couldn't be removed."""
    import shlex
    uid = os.getuid()
    user = [e for e in entries if not e["system"]]
    system = [e for e in entries if e["system"]]
    for e in entries:
        if e["kind"] != "System daemon":
            subprocess.run(["/bin/launchctl", "bootout", "gui/{}/{}".format(uid, e["label"])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failed = trash_paths([e["path"] for e in user], finder_fallback=False, label="Remove startup items", batch_id=batch_id)
    if system:
        trash = os.path.join(HOME, ".Trash")
        moves, lines = {}, []
        stamp = time.strftime("%H.%M.%S")
        for i, e in enumerate(system):
            folder = "daemon" if e["kind"] == "System daemon" else "agent"
            dest = os.path.join(trash, "{} {} {} {}".format(stamp, i + 1, folder, os.path.basename(e["path"])))
            moves[e["path"]] = dest
            if e["kind"] == "System daemon":
                lines.append("launchctl bootout system {} 2>/dev/null".format(shlex.quote(e["path"])))
            lines.append("mv -n {} {} && chown {} {}".format(shlex.quote(e["path"]), shlex.quote(dest), uid, shlex.quote(dest)))
        ok, _ = run_as_admin("; ".join(lines), "Burrow needs your password to remove {} startup item{}.".format(len(system), "" if len(system) == 1 else "s"))
        done = {k: v for k, v in moves.items() if not os.path.lexists(k) and os.path.lexists(v)}
        if done:
            record_trash_batch("Remove startup items", done, batch_id)
        failed += [k for k in moves if os.path.lexists(k)]
    return failed


# ---------------------------------------------------------------------------
# Duplicate files
# ---------------------------------------------------------------------------

DUPE_MIN_SIZE = 1024 * 1024
# Packages look like folders but are single documents; their insides must never be split up.
DUPE_SKIP_SUFFIXES = (
    ".app", ".bundle", ".framework", ".plugin", ".kext", ".appex", ".xpc", ".prefPane", ".qlgenerator", ".mdimporter",
    ".photoslibrary", ".musiclibrary", ".tvlibrary", ".aplibrary", ".lrlibrary", ".lrdata", ".fcpbundle", ".imovielibrary",
    ".xcarchive", ".xcodeproj", ".xcworkspace", ".playground", ".docset",
    ".sparsebundle", ".dmgpart", ".utm", ".pvm", ".vmwarevm", ".vbox", ".parallels",
    ".logicx", ".band", ".garageband", ".rtfd", ".pages", ".numbers", ".key", ".scriptd", ".workflow",
    ".pkg", ".mpkg", ".sketch", ".cptx", ".dtbase2", ".dtsparse", ".epub", ".backup", ".photoboothlibrary",
)
SF_DATALESS = 0x40000000  # iCloud file whose contents aren't downloaded


def is_package_dir(path, name):
    lower = name.lower()
    if lower.endswith(tuple(s.lower() for s in DUPE_SKIP_SUFFIXES)):
        return True
    ext = lower.rsplit(".", 1)[1] if "." in lower else ""
    # App libraries and bundles named like ".libCapto", ".photolibrary", ".mybundle"
    if ext and (ext.startswith("lib") or ext.endswith(("library", "bundle", "pkg", "package", "db"))):
        return True
    # Unknown package types usually carry an Info.plist or a Contents folder at the top.
    return "." in name and (os.path.exists(os.path.join(path, "Info.plist")) or os.path.isdir(os.path.join(path, "Contents")))


def default_dupe_roots():
    return [p for p in (os.path.join(HOME, n) for n in ("Downloads", "Desktop", "Documents", "Movies", "Music", "Pictures")) if os.path.isdir(p)]


def _hash_file(path, limit=None):
    import hashlib
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            remaining = limit
            while True:
                chunk = f.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
                if not chunk:
                    break
                h.update(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        break
    except OSError:
        return None
    return h.hexdigest()


def verify_duplicates(keep, candidates):
    """Of candidates, return those still byte-identical to `keep`."""
    try:
        size = os.stat(keep).st_size
    except OSError:
        return []
    digest = _hash_file(keep)
    if digest is None:
        return []
    same = []
    for c in candidates:
        try:
            if c == keep or os.path.islink(c) or os.stat(c).st_size != size:
                continue
            if os.path.samefile(c, keep):
                continue
        except OSError:
            continue
        if _hash_file(c) == digest:
            same.append(c)
    return same


def duplicates(roots, min_size=DUPE_MIN_SIZE):
    """Yield progress and groups of identical files (same size, then same content)."""
    by_size = {}
    seen_inodes = set()
    scanned = 0
    for root in roots:
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not is_package_dir(e.path, e.name) and e.name not in ("node_modules", "Library"):
                            stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_size < min_size or (st.st_dev, st.st_ino) in seen_inodes:
                    continue
                if getattr(st, "st_flags", 0) & SF_DATALESS:
                    continue  # reading it would download it from iCloud
                seen_inodes.add((st.st_dev, st.st_ino))
                by_size.setdefault(st.st_size, []).append(e.path)
                scanned += 1
                if scanned % 2000 == 0:
                    yield {"type": "progress", "files": scanned}
    yield {"type": "progress", "files": scanned, "hashing": True}
    for size, paths in sorted(by_size.items(), key=lambda kv: -kv[0]):
        if len(paths) < 2:
            continue
        by_head = {}
        for p in paths:
            h = _hash_file(p, 64 * 1024)
            if h:
                by_head.setdefault(h, []).append(p)
        for group in by_head.values():
            if len(group) < 2:
                continue
            by_full = {}
            for p in group:
                h = _hash_file(p)
                if h:
                    by_full.setdefault(h, []).append(p)
            for digest, same in by_full.items():
                if len(same) > 1:
                    yield {"type": "group", "id": digest[:16], "size": size, "paths": sorted(same)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv):
    if not argv:
        sys.stderr.write(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "clean-scan":
        total = count = 0
        for it in clean_scan():
            emit_line(it)
            if it["type"] == "item":
                total += it["size"]
                count += 1
        emit_line({"type": "summary", "total": total, "items": count})
    elif cmd == "purge-scan":
        min_days = int(args[0]) if args else None
        total = count = 0
        for it in purge_scan(min_days):
            emit_line(it)
            total += it["size"]
            count += 1
        emit_line({"type": "summary", "total": total, "items": count})
    elif cmd == "analyze":
        for it in analyze(args[0] if args else "."):
            emit_line(it)
    elif cmd == "app-sizes":
        app_sizes(args[0])
    elif cmd == "optimize":
        if not args:
            print(json.dumps(optimizations(), indent=2))
        else:
            if args[0] == "--all":
                ids = [o["id"] for o in optimizations() if o["default"]]
            else:
                ids = [a for a in args if not a.startswith("--")]  # "--run dns fonts" or "dns fonts"
            print(json.dumps(run_optimizations(ids)))
    elif cmd == "touchid":
        sub = args[0] if args else "status"
        if sub == "status":
            print(json.dumps(touchid_status()))
        else:
            ok, out = touchid_set(sub == "enable")
            print(json.dumps({"ok": ok, "output": out}))
    elif cmd == "updates":
        import updates
        print(json.dumps(updates.check_all(), indent=2))
    elif cmd == "leftovers":
        print(json.dumps(orphaned_leftovers(), indent=2))
    elif cmd == "startup":
        print(json.dumps(startup_items(), indent=2))
    elif cmd == "dupes":
        roots = [os.path.abspath(os.path.expanduser(a)) for a in args] or default_dupe_roots()
        for it in duplicates(roots):
            emit_line(it)
        emit_line({"type": "done"})
    elif cmd == "large":
        print(json.dumps(large_files(int(args[0]) if args else 500 * 1024 * 1024), indent=2))
    elif cmd == "trash":
        print(json.dumps({"failed": trash_paths(args)}))
    else:
        sys.stderr.write(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
