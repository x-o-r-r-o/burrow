#!/usr/bin/python3
"""Burrow: an Alfred workflow for deep cleaning and optimizing your Mac.

This file is the Alfred side: Script Filters call it with a command name and
the query, and the single Run Script action calls it with `run`, reading what
to do from the `action` / `target` / `payload` variables the selected item set.
The scanning and maintenance work lives in engine.py.

Written for the system /usr/bin/python3 (3.9), so no 3.10+ syntax.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time

import engine
from engine import HOME, format_bytes

WF_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(WF_DIR, "engine.py")
PYTHON = "/usr/bin/python3"
ICONS = os.path.join(WF_DIR, "icons")
BUNDLE_ID = engine.BUNDLE_ID
CACHE_DIR = engine.CACHE_DIR

DEFAULT_KEYWORDS = {
    "hub": "burrow",
    "status": "bustatus",
    "clean": "buclean",
    "optimize": "buoptimize",
    "uninstall": "buuninstall",
    "purge": "bupurge",
    "analyze": "buanalyze",
    "installer": "buinstallers",
    "touchid": "butouchid",
    "menubar": "bumenu",
    "large": "bularge",
    "startup": "bustartup",
    "dupes": "budupes",
    "updates": "buupdates",
    "browsers": "bubrowsers",
    "processes": "bukill",
}


def load_keywords():
    """The keywords as configured in Alfred (read from the workflow's info.plist),
    so a keyword the user renamed in Alfred Preferences keeps working everywhere."""
    words = dict(DEFAULT_KEYWORDS)
    try:
        import plistlib
        with open(os.path.join(WF_DIR, "info.plist"), "rb") as f:
            info = plistlib.load(f)
        defaults = {c.get("variable"): (c.get("config") or {}).get("default") for c in info.get("userconfigurationconfig", [])}
        for obj in info.get("objects", []):
            cfg = obj.get("config", {})
            m = re.search(r"run\.sh (\w+) ", cfg.get("script", ""))
            word = cfg.get("keyword") or ""
            var = re.fullmatch(r"\{var:(\w+)\}", word)
            if var:  # set in the Workflow's Configuration; Alfred passes it to scripts
                word = os.environ.get(var.group(1)) or defaults.get(var.group(1)) or ""
            if obj.get("type", "").endswith("scriptfilter") and m and word.strip():
                words[m.group(1)] = word.strip()
    except Exception:  # noqa: BLE001 — fall back to the defaults
        pass
    return words


KEYWORDS = load_keywords()

# How long a finished scan is reused before a fresh one starts (seconds).
SCAN_TTL = 10 * 60
ANALYZE_TTL = 15 * 60
APP_SIZES_TTL = 24 * 60 * 60


def format_percent(v):
    return "{:.1f}%".format(v or 0)


def format_rate(mb_per_sec):
    mb_per_sec = mb_per_sec or 0
    if mb_per_sec < 0.001:
        return "0 KB/s"
    if mb_per_sec < 1:
        return "{:.0f} KB/s".format(mb_per_sec * 1024)
    return "{:.1f} MB/s".format(mb_per_sec)


def tilde(path):
    if path == HOME:
        return "~"
    return "~" + path[len(HOME):] if path.startswith(HOME + "/") else path


def plural(n, word):
    return "{} {}{}".format(n, word, "" if n == 1 else ("es" if word.endswith(("s", "x", "ch", "sh")) else "s"))


# ---------------------------------------------------------------------------
# Background jobs: long scans run detached and stream JSON lines to a cache
# file; the Script Filter re-runs itself (Alfred `rerun`) and shows partial
# results while the scan is still going.
# ---------------------------------------------------------------------------


class Job:
    def __init__(self, name):
        self.dir = os.path.join(CACHE_DIR, "jobs")
        os.makedirs(self.dir, exist_ok=True)
        self.out = os.path.join(self.dir, name + ".out")
        self.done = os.path.join(self.dir, name + ".done")
        self.pidfile = os.path.join(self.dir, name + ".pid")

    def _pid(self):
        try:
            with open(self.pidfile) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _alive(self):
        """True only if the recorded pid is still this job's shell. Pids get reused,
        so a bare existence check could match (and later signal) someone else's process."""
        pid = self._pid()
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=engine.TOOL_ENV).stdout.decode("utf-8", "replace")
        return ENGINE in cmd

    def state(self):
        if os.path.exists(self.done):
            return "done"
        if os.path.exists(self.pidfile):
            if self._alive():
                return "running"
            if self._pid() is None and time.time() - os.path.getmtime(self.pidfile) < 5:
                return "running"  # launched a moment ago; the shell hasn't written its pid yet
            with open(self.done, "w") as f:  # vanished without an exit code
                f.write("-1")
            return "done"
        return "none"

    def started_at(self):
        for p in (self.pidfile, self.out):
            if os.path.exists(p):
                return os.path.getmtime(p)
        return time.time()

    def age(self):
        return time.time() - os.path.getmtime(self.done) if os.path.exists(self.done) else 0

    def exit_code(self):
        try:
            with open(self.done) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def output(self):
        try:
            with open(self.out, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def records(self):
        """Complete JSON lines written so far (a half-written last line is skipped)."""
        out = []
        for line in self.output().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def start(self, argv):
        self.clear()
        open(self.out, "w").close()
        # The shell records its own pid first, so the job is tracked even if Alfred
        # cancels this script (queue mode "terminate previous") right after launching it.
        with open(self.pidfile, "w") as f:
            f.write("")
        subprocess.Popen(
            ["/bin/sh", "-c", 'echo $$ > "$JOB_PID"; "$@" > "$JOB_OUT" 2>"$JOB_OUT.err"; echo $? > "$JOB_DONE"', "sh"] + list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, JOB_OUT=self.out, JOB_DONE=self.done, JOB_PID=self.pidfile),
            start_new_session=True,
        )

    def error_text(self):
        try:
            with open(self.out + ".err") as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            return lines[-1] if lines else ""
        except OSError:
            return ""

    def kill(self):
        pid = self._pid()
        if pid and not os.path.exists(self.done) and self._alive():
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass

    def clear(self):
        self.kill()
        for p in (self.out, self.out + ".err", self.done, self.pidfile):
            try:
                os.remove(p)
            except OSError:
                pass

    def ensure(self, argv, ttl=SCAN_TTL, timeout=None):
        """Start the job if it never ran or its result is stale; enforce timeout."""
        state = self.state()
        if state == "none" or (state == "done" and self.age() > ttl):
            self.start(argv)
            return "running"
        if state == "running" and timeout and time.time() - self.started_at() > timeout:
            self.kill()
            with open(self.done, "w") as f:
                f.write("124")
            return "done"
        return state

    def failure(self):
        code = self.exit_code()
        if code in (0, None):
            return None
        if code == 124:
            return "Timed out"
        return self.error_text() or "Exited with code {}".format(code)


def job_for(kind, key=""):
    name = kind if not key else "{}-{}".format(kind, hashlib.sha1(key.encode()).hexdigest()[:12])
    return Job(name)


def engine_argv(*args):
    return [PYTHON, ENGINE] + list(args)


# ---------------------------------------------------------------------------
# Alfred output helpers
# ---------------------------------------------------------------------------


def icon(name):
    return {"path": os.path.join(ICONS, name + ".png")}


def file_icon(path):
    return {"type": "fileicon", "path": path}


def usage_color(percent):
    if percent < 50:
        return "green"
    if percent < 75:
        return "yellow"
    if percent < 90:
        return "orange"
    return "red"


def health_color(score):
    if score >= 90:
        return "green"
    if score >= 70:
        return "yellow"
    if score >= 50:
        return "orange"
    return "red"


def act(action, target="", **payload):
    """Variables that tell the Run Script what to do."""
    return {"action": action, "target": target, "payload": json.dumps(payload) if payload else ""}


def mod(subtitle, variables, arg=None, valid=True):
    return {"subtitle": subtitle, "valid": valid, "arg": arg if arg is not None else variables.get("target", ""), "variables": variables}


def item(title, subtitle="", icn=None, variables=None, mods=None, valid=True, autocomplete=None, arg=None, text=None, quicklook=None, uid=None):
    it = {"title": title, "subtitle": subtitle, "valid": valid}
    if icn:
        it["icon"] = icn
    if variables is not None:
        it["variables"] = variables
        it["arg"] = arg if arg is not None else variables.get("target", "")
    elif arg is not None:
        it["arg"] = arg
    if mods:
        it["mods"] = mods
    if autocomplete is not None:
        it["autocomplete"] = autocomplete
    if text:
        it["text"] = text
    if quicklook:
        it["quicklookurl"] = quicklook
    if uid:
        it["uid"] = uid
    return it


def emit(items, rerun=None, learn=False):
    """learn=True lets Alfred reorder by what you pick (hub, app list); status rows,
    scan results and navigation keep Burrow's order."""
    out = {"items": items, "skipknowledge": not learn}
    if rerun:
        out["rerun"] = rerun
    sys.stdout.write(json.dumps(out))


def matches(query, *fields):
    if not query:
        return True
    hay = " ".join(f for f in fields if f).lower()
    return all(word in hay for word in query.lower().split())


def rescan(kind, command, key="", query=""):
    return act("rescan", KEYWORDS[command] + " " + query, kind=kind, key=key)


def elapsed(job):
    return int(time.time() - job.started_at())


# ---------------------------------------------------------------------------
# Space freed over time
# ---------------------------------------------------------------------------


def stats():
    return engine.load_state("stats.json") or {"freed": 0, "runs": 0}


def record_freed(nbytes):
    if nbytes <= 0:
        return

    def change(st):
        st["freed"] = st.get("freed", 0) + int(nbytes)
        st["runs"] = st.get("runs", 0) + 1
        st["last"] = time.time()
        return st
    engine.update_state("stats.json", change)


def disk_free():
    st = os.statvfs("/")
    return st.f_bavail * st.f_frsize


# ---------------------------------------------------------------------------
# bu — command hub
# ---------------------------------------------------------------------------

HUB = [
    ("status", "System Status", "Health score, CPU, memory, disk, battery and network", "status"),
    ("updates", "App Updates", "Check your apps for new versions and install them safely", "update"),
    ("clean", "Clean System", "Move caches, logs and temporary files to the Trash", "clean"),
    ("browsers", "Browsers", "Clear history, cache, cookies and more, or reset a browser", "browser"),
    ("optimize", "Optimize System", "Flush DNS, rebuild databases, refresh services", "optimize"),
    ("processes", "Processes", "See what's using CPU and memory, and quit or force quit it", "process"),
    ("uninstall", "Uninstall App", "Remove apps and their leftover files", "uninstall"),
    ("analyze", "Analyze Disk", "Browse folders sorted by size", "analyze"),
    ("large", "Large Files", "Find the biggest files in your home folder", "large"),
    ("dupes", "Duplicate Files", "Find identical copies in Downloads, Documents, Desktop and media folders", "dupes"),
    ("startup", "Startup Items", "Launch agents and daemons, including ones left behind by deleted apps", "startup"),
    ("purge", "Purge Dev Artifacts", "Remove old node_modules, .next, dist, target, venv…", "purge"),
    ("installer", "Clean Installers", "Remove .dmg, .pkg and .iso files", "installer"),
    ("touchid", "Touch ID for Sudo", "Use your fingerprint instead of a password for sudo", "touchid-green"),
    ("menubar", "Burrow Companion", "Optional app: health score in the menu bar, Updates and Browsers windows", "menubar"),
]


def last_scan_summaries():
    """"8.8 GB found 5 minutes ago" for scans that have finished recently."""
    out = {}
    try:
        import updates
        n = len(updates.visible_updates())
        if n:
            out["updates"] = "{} available".format(plural(n, "update"))
    except Exception:  # noqa: BLE001
        pass
    for key, kind in (("clean", "clean"), ("purge", "purge")):
        job = job_for(kind)
        if job.state() == "done" and not job.failure() and job.age() < 3600:
            total = sum(r["size"] for r in job.records() if r.get("type") == "item" and not r.get("optional"))
            if total:
                out[key] = "{} to clean, found {}".format(format_bytes(total), time_since(os.path.getmtime(job.done)))
    return out


def cmd_hub(query):
    items = []
    free = format_bytes(disk_free())
    found = last_scan_summaries()
    for key, title, sub, icn in HUB:
        if key in found:
            sub = found[key]
        elif key == "analyze":
            sub = "{} · {} free".format(sub, free)
        if matches(query, title, sub, KEYWORDS[key]):
            items.append(item(title, "{} · {}".format(sub, KEYWORDS[key]), icon(icn), act("alfred_search", KEYWORDS[key] + " "),
                              autocomplete=None, uid="hub-" + key))
    undo = undo_item()
    if undo and matches(query, undo["title"], "undo put back"):
        undo.pop("uid", None)  # keep it on top: Alfred doesn't reorder rows without a uid
        items.insert(0, undo)
    freed = stats().get("freed", 0)
    if freed and not query:
        items.append(item("Burrow has freed {}".format(format_bytes(freed)), "Total moved to the Trash by Burrow so far", icon("check"), valid=False))
    trash = empty_trash_item()
    if trash and matches(query, "Empty Trash", "free space"):
        items.append(trash)
    emit(items or [item("No matching Burrow command", "Clear the search to see every command", icon("search"), valid=False, autocomplete="")], learn=True)


def undo_item():
    batch = engine.last_trash_batch()
    if not batch:
        return None
    left = sum(1 for i in batch["items"] if os.path.lexists(i["to"]))
    return item(
        "Undo: {}".format(batch["label"]),
        "Put {} back where {} came from · {}".format(plural(left, "item"), "it" if left == 1 else "they", time_since(batch["time"])),
        icon("undo"), act("undo"), uid="undo",
    )


def empty_trash_item():
    size = engine.trash_size()
    if size == 0:
        return None  # nothing to empty (None means macOS won't tell us, so still offer it)
    return item(
        "Empty Trash" + ("  —  {}".format(format_bytes(size)) if size else ""),
        "Permanently delete everything in the Trash to free the space",
        icon("trash"),
        act("empty_trash"),
        uid="hub-empty-trash",
    )


# ---------------------------------------------------------------------------
# bustatus — System Status
# ---------------------------------------------------------------------------


def cmd_status(query):
    try:
        refresh = float(os.environ.get("status_refresh", "3") or 3)
    except ValueError:
        refresh = 3
    rerun = max(1.0, min(5.0, refresh))  # Alfred allows 0.1–5s

    try:
        data = engine.status()
    except Exception as e:  # noqa: BLE001 — surface anything to the user
        emit([item("Failed to Get System Status", str(e), icon("error"), act("noop"))], rerun=rerun)
        return

    hw = data["hardware"]
    cpu = data["cpu"]
    mem = data["memory"]
    power = data["thermal"]
    activity = act("open_app", "Activity Monitor")
    items = []

    score = data["health_score"]
    overview = "Health: {}/100 — {}\nModel: {} — {}\nOS: {}\nUptime: {}\nHost: {}\nProcesses: {}".format(
        score, data["health_score_msg"], hw["model"], hw["cpu_model"], hw["os_version"], data["uptime"], data["host"], data["procs"]
    )
    items.append(item(
        "Health Score  {}/100".format(score),
        "{} · {} · up {}".format(data["health_score_msg"], hw["model"], data["uptime"]),
        icon("health-" + health_color(score)), activity,
        text={"copy": overview, "largetype": overview},
    ))

    usage = cpu["usage"]
    load = "{:.2f} / {:.2f} / {:.2f}".format(cpu["load1"], cpu["load5"], cpu["load15"])
    cores = "{} cores".format(cpu["core_count"])
    if cpu["p_core_count"] and cpu["e_core_count"]:
        types = cpu.get("core_types") or ["P", "E"]
        cores += " ({} {} + {} {})".format(cpu["p_core_count"], types[0], cpu["e_core_count"], types[1])
    per_core = "  ".join("{}: {:.0f}%".format(i + 1, u) for i, u in enumerate(cpu["per_core"]))
    cpu_detail = "CPU {} — {}\nLoad {} · {}\nPer core: {}".format(format_percent(usage), hw["cpu_model"], load, cores, per_core)
    items.append(item(
        "CPU  {}".format(format_percent(usage)),
        "{} · Load {} · {}".format(hw["cpu_model"], load, cores),
        icon("cpu-" + usage_color(usage)), activity,
        text={"copy": cpu_detail, "largetype": cpu_detail},
    ))

    for gi, gpu in enumerate(data["gpu"]):
        items.append(item(
            "GPU  {}".format(format_percent(gpu["usage"])),
            "{}{}".format(gpu["name"], " · {} cores".format(gpu["core_count"]) if gpu.get("core_count") else ""),
            icon("gpu-" + usage_color(gpu["usage"])), activity,
        ))

    m_pct = mem["used_percent"]
    mem_detail = "Memory {}\nUsed: {} / {}\nAvailable: {}\nCached files: {}\nSwap: {} / {}\nPressure: {}".format(
        format_percent(m_pct), format_bytes(mem["used"]), format_bytes(mem["total"]),
        format_bytes(mem["total"] - mem["used"]), format_bytes(mem["cached"]),
        format_bytes(mem["swap_used"]), format_bytes(mem["swap_total"]), mem["pressure"],
    )
    items.append(item(
        "Memory  {}".format(format_percent(m_pct)),
        "{} / {} · Swap {} · Pressure {}".format(format_bytes(mem["used"]), hw["total_ram"], format_bytes(mem["swap_used"]), mem["pressure"]),
        icon("memory-" + usage_color(m_pct)), activity,
        text={"copy": mem_detail, "largetype": mem_detail},
    ))

    for disk in data["disks"]:
        d_pct = disk["used_percent"]
        free = disk.get("available") or disk["total"] - disk["used"]
        detail = "Disk {}\nDevice: {}\nUsed: {} ({})\nFree: {}\nTotal: {}\nType: {}".format(
            disk["mount"], disk["device"], format_bytes(disk["used"]), format_percent(d_pct), format_bytes(free), format_bytes(disk["total"]), disk["fstype"].upper()
        )
        items.append(item(
            "Disk {}  {}".format("Macintosh HD" if disk["mount"] == "/" else os.path.basename(disk["mount"]), format_percent(d_pct)),
            "{} free of {} · {}{} · ↩ Analyze".format(format_bytes(free), format_bytes(disk["total"]), disk["fstype"].upper(), " · External" if disk["external"] else ""),
            icon("disk-" + usage_color(d_pct)),
            act("alfred_search", KEYWORDS["analyze"] + " " + (tilde(HOME) + "/" if disk["mount"] == "/" else disk["mount"] + "/")),
            text={"copy": detail, "largetype": detail},
        ))

    io = data["disk_io"]
    items.append(item("Disk I/O", "Read {} · Write {}".format(format_rate(io["read_rate"]), format_rate(io["write_rate"])), icon("disk-green"), activity))

    for bat in data["batteries"]:
        pct = bat["percent"]
        color = "green" if pct > 20 else "orange" if pct > 10 else "red"
        bits = [bat["status"].capitalize()]
        if bat["time_left"]:
            bits.append(bat["time_left"] + (" to full" if bat["status"] == "charging" else " left"))
        bits += ["Capacity {}%".format(bat["capacity"]) if bat["capacity"] else "", plural(bat["cycle_count"], "cycle"), bat["health"]]
        detail = "Battery {}%\n{}".format(pct, "\n".join(b for b in bits if b))
        items.append(item(
            "Battery  {}%".format(pct), " · ".join(b for b in bits if b),
            icon(("battery-charging-" if bat["status"] == "charging" else "battery-") + color),
            act("open", "x-apple.systempreferences:com.apple.Battery-Settings.extension"),
            text={"copy": detail, "largetype": detail},
        ))

    temp_bits = []
    if power.get("cpu_temp"):
        temp_bits.append("CPU {:.0f}°C".format(power["cpu_temp"]))
    if power.get("gpu_temp"):
        temp_bits.append("GPU {:.0f}°C".format(power["gpu_temp"]))
    if power.get("battery_temp"):
        temp_bits.append("Battery {:.0f}°C".format(power["battery_temp"]))
    if power.get("fans"):
        temp_bits.append("Fans " + " / ".join("{} rpm".format(f) for f in power["fans"]))
    if temp_bits:
        hot = power.get("cpu_temp", 0)
        color = "green" if hot < 70 else "yellow" if hot < 85 else "orange" if hot < 95 else "red"
        items.append(item(
            "Temperature  {:.0f}°C".format(hot) if hot else "Fans",
            " · ".join(temp_bits), icon("temp-" + color), activity,
        ))

    power_bits = []
    if power.get("system_power"):
        power_bits.append("Mac uses {:.1f} W".format(power["system_power"]))
    if power.get("battery_power"):
        bp = power["battery_power"]
        power_bits.append("battery {} {:.1f} W".format("charging" if bp > 0 else "draining", abs(bp)))
    if power.get("adapter_power"):
        power_bits.append("{} W adapter".format(power["adapter_power"]))
    if power_bits:
        items.append(item("Power", " · ".join(power_bits), icon("thermal"), activity))

    for iface in data["network"]:
        items.append(item(
            "{}  {}".format(iface["name"], iface["ip"]),
            "↓ {} · ↑ {} · ↩ Copy IP".format(format_rate(iface["rx_rate_mbs"]), format_rate(iface["tx_rate_mbs"])),
            icon("network"), act("copy", iface["ip"]), text={"copy": iface["ip"]},
        ))
    if data["proxy"]["enabled"]:
        items.append(item("Proxy", "{} · {}".format(data["proxy"]["type"], data["proxy"]["host"]), icon("network"), valid=False))

    for i, proc in enumerate(data["top_processes"]):
        items.append(item(
            proc["name"], "Top process · CPU {} · Memory {} · ↩ Show in Processes".format(format_percent(proc["cpu"]), format_percent(proc["memory"])),
            icon("process"), act("go", "processes " + proc["name"]),
            mods={"cmd": mod("Open Activity Monitor", activity)},
        ))

    items = [i for i in items if matches(query, i["title"], i["subtitle"])]
    emit(items or [item("No matching status entries", "", icon("search"), valid=False)], rerun=rerun)


# ---------------------------------------------------------------------------
# buclean — Clean System
# ---------------------------------------------------------------------------


def clean_results(job):
    items, note = [], None
    for r in job.records():
        if r.get("type") == "item":
            items.append(r)
        elif r.get("type") == "note":
            note = r
    return items, note


def cmd_clean(query):
    job = job_for("clean")
    state = job.ensure(engine_argv("clean-scan"), timeout=300)
    running = state == "running"
    found, note = clean_results(job)
    total = sum(i["size"] for i in found if not i.get("optional"))
    rescan_vars = rescan("clean", "clean")
    items = []

    failure = None if running else job.failure()
    if failure:
        emit([item("Scan Failed", failure + " · ↩ Try again", icon("error"), rescan_vars)])
        return

    clean_all = act("clean_all", "", total=total, count=sum(1 for i in found if not i.get("optional")))
    if running:
        sub = "{} found so far".format(format_bytes(total)) if total else "Looking for caches, logs and temporary files"
        items.append(item("Scanning… {}s".format(elapsed(job)), sub, icon("search"), valid=False))
    elif not found:
        items.append(item("Nothing to Clean", "Your caches and logs are already tidy · ↩ Rescan", icon("check"), rescan_vars))
    else:
        items.append(item(
            "Total Recoverable Space  {}".format(format_bytes(total)),
            "{} · ↩ Clean all".format(plural(sum(1 for i in found if not i.get("optional")), "item")),
            icon("trash"), clean_all,
        ))

    for it in sorted(found, key=lambda i: -i["size"]) if not running else found:
        if not matches(query, it["description"], it["section"]):
            continue
        sub = [it["section"] + (" · not in Clean all" if it.get("optional") else "")]
        if it.get("note"):
            sub.append(it["note"])
        if len(it["paths"]) > 1:
            sub.append(plural(len(it["paths"]), "location"))
        one = act("clean_items", "", keys=[it["paths"][0]], size=it["size"])
        items.append(item(
            "{}  —  {}".format(it["description"], format_bytes(it["size"])),
            " · ".join(sub) + ("" if running else " · ↩ Clean this"),
            icon("doc"),
            None if running else one,
            mods=None if running else {
                "cmd": mod("Reveal in Finder", act("reveal", it["paths"][0])),
                "alt": mod("Clean this", one),
                "ctrl": mod("Never clean this", act("clean_ignore", it["paths"][0], description=it["description"])),
            },
            valid=not running,
            text={"copy": "\n".join(it["paths"]), "largetype": "\n".join(tilde(p) for p in it["paths"][:20])},
        ))

    if not running:
        if note:
            items.append(item(
                "Skipped {} (running)".format(plural(note["count"], "app")),
                "Quit them and rescan to include their caches · ⌘L to see which",
                icon("info"), rescan_vars, text={"largetype": note["message"], "copy": note["message"]},
            ))
        ignored = engine.clean_ignored()
        if ignored:
            items.append(item(
                "{} Never Cleaned".format(plural(len(ignored), "Item")),
                "You chose to keep these · ↩ Include them again · ⌘L to see which",
                icon("hidden"), act("clean_unignore"),
                text={"largetype": "\n".join(tilde(x) for x in sorted(ignored)), "copy": "\n".join(sorted(ignored))},
            ))
        items.append(item("Rescan", "Scan caches and logs again", icon("refresh"), rescan_vars))
        undo = undo_item()
        if undo:
            items.append(undo)
        trash = empty_trash_item()
        if trash:
            items.append(trash)
    emit(items, rerun=0.5 if running else None)


# ---------------------------------------------------------------------------
# buoptimize — Optimize System
# ---------------------------------------------------------------------------


def cmd_optimize(query):
    tasks = engine.optimizations()
    defaults = [t for t in tasks if t["default"]]
    admin = any(t["admin"] for t in defaults)
    items = [item(
        "Optimize All  —  {}".format(plural(len(defaults), "task")),
        "Runs every task marked ✓{}".format(" · 🔒 asks for your password once" if admin else ""),
        icon("optimize"),
        act("optimize", "", ids=[t["id"] for t in defaults], confirm=True),
    )]
    for t in tasks:
        if not matches(query, t["title"], t["section"], t["detail"]):
            continue
        flags = ["✓ in Optimize All" if t["default"] else "not in Optimize All"]
        if t["admin"]:
            flags.append("🔒")
        items.append(item(
            t["title"],
            "{} · {}".format(t["detail"], " · ".join(flags)),
            icon("item" if t["default"] else "optimize"),
            act("optimize", "", ids=[t["id"]], confirm=t["id"] in ("spotlight", "finder")),
            uid="opt-" + t["id"],
        ))
    emit(items)


# ---------------------------------------------------------------------------
# bupurge — Purge Dev Artifacts
# ---------------------------------------------------------------------------


def age_label(days):
    return "today" if days == 0 else "1 day ago" if days == 1 else "{} days ago".format(days)


def cmd_purge(query):
    job = job_for("purge")
    signature = "{}|{}".format(os.environ.get("project_dirs", ""), os.environ.get("purge_min_days", ""))
    if engine.load_state("purge-settings.json").get("sig") != signature:
        job.clear()  # folders or minimum age changed: old results no longer apply
        engine.save_state("purge-settings.json", {"sig": signature})
    state = job.ensure(engine_argv("purge-scan"), timeout=300)
    running = state == "running"
    found = [r for r in job.records() if r.get("type") == "item"]
    found.sort(key=lambda r: -r["size"])
    total = sum(r["size"] for r in found)
    rescan_vars = rescan("purge", "purge")
    items = []

    failure = None if running else job.failure()
    if failure:
        emit([item("Scan Failed", failure + " · ↩ Try again", icon("error"), rescan_vars)])
        return

    purge_all = act(
        "trash_many", "", paths=[r["path"] for r in found], confirm="Purge All Artifacts",
        message="This will move {} of build artifacts from {} to the Trash. You can rebuild them with your package manager.".format(format_bytes(total), plural(len(found), "project")),
        bytes=total, button="Purge All", done="Artifacts purged — {} moved to the Trash".format(format_bytes(total)),
        invalidate=["purge"], reopen=KEYWORDS["purge"] + " ",
    )
    try:
        min_days = int(os.environ.get("purge_min_days", "7") or 7)
    except ValueError:
        min_days = 7
    roots = ", ".join(tilde(r) for r in engine.project_roots()[:4]) or "no project folders found"

    if running:
        items.append(item("Scanning projects… {}s".format(elapsed(job)), "Found {} so far".format(format_bytes(total)) if found else "Looking in " + roots, icon("search"), valid=False))
    elif not found:
        items.append(item("No Build Artifacts Found", "Nothing older than {} in {} · ↩ Rescan".format(plural(min_days, "day"), roots), icon("check"), rescan_vars))
    else:
        items.append(item(
            "Total Recoverable Space  {}".format(format_bytes(total)),
            "{} older than {} · ↩ Purge all".format(plural(len(found), "artifact"), plural(min_days, "day")),
            icon("trash"), purge_all,
        ))

    for r in found:
        if not matches(query, r["project"], r["artifact"], r["path"]):
            continue
        items.append(item(
            "{}  —  {}".format(r["project"], format_bytes(r["size"])),
            "{} · touched {} · {}".format(r["artifact"], age_label(r["days"]), tilde(r["path"])),
            icon("purge"),
            act("trash", r["path"], confirm="Remove {}?".format(r["artifact"]),
                message="This will move {} from {} ({}) to the Trash.".format(r["artifact"], r["project"], format_bytes(r["size"])),
                button="Remove", bytes=r["size"], done="{} removed from {}".format(r["artifact"], r["project"]),
                invalidate=["purge"], reopen=KEYWORDS["purge"] + " "),
            mods=None if running else {
                "cmd": mod("Reveal in Finder", act("reveal", r["path"])),
                "alt": mod("Move to Trash", act("trash", r["path"], confirm="Remove {}?".format(r["artifact"]),
                           message="This will move {} from {} ({}) to the Trash.".format(r["artifact"], r["project"], format_bytes(r["size"])),
                           button="Remove", bytes=r["size"], done="{} removed from {}".format(r["artifact"], r["project"]),
                           invalidate=["purge"], reopen=KEYWORDS["purge"] + " ")),
                "ctrl": mod("Copy path", act("copy", r["path"])),
            },
            text={"copy": r["path"], "largetype": r["path"]},
            quicklook=r["path"],
            valid=not running,
        ))
    if not running:
        items.append(item("Rescan", "Search project folders again · set folders in the Workflow’s Configuration", icon("refresh"), rescan_vars))
    emit(items, rerun=0.5 if running else None)


# ---------------------------------------------------------------------------
# buinstallers — Clean Installers
# ---------------------------------------------------------------------------


def cmd_installer(query):
    files = engine.find_installers()
    if not files:
        emit([item("No Installers Found", "No .dmg, .pkg, or .iso files in Downloads, Desktop, or Documents.", icon("check"), valid=False)])
        return
    total = sum(f["size"] for f in files)
    remove_all = act(
        "trash_many", "", paths=[f["path"] for f in files], confirm="Remove All Installers",
        message="This will move {} ({}) to the Trash.".format(plural(len(files), "installer file"), format_bytes(total)),
        button="Remove All", bytes=total, done="Installers removed — {} moved to the Trash".format(format_bytes(total)), reopen=KEYWORDS["installer"] + " ",
    )
    items = [item("Installers  —  {}".format(format_bytes(total)), "{} · ↩ Move all to the Trash".format(plural(len(files), "file")), icon("trash"), remove_all)]
    for f in files:
        if not matches(query, f["name"], f["location"], f["ext"]):
            continue
        items.append(item(
            f["name"],
            "{} · {} · {}".format(f["ext"], format_bytes(f["size"]), f["location"]),
            file_icon(f["path"]),
            act("trash", f["path"], confirm="Remove {}?".format(f["name"]),
                message="This will move {} ({}) to the Trash.".format(f["name"], format_bytes(f["size"])),
                button="Remove", bytes=f["size"], done="{} removed".format(f["name"]), reopen=KEYWORDS["installer"] + " "),
            mods={"cmd": mod("Reveal in Finder", act("reveal", f["path"])), "ctrl": mod("Copy path", act("copy", f["path"]))},
            text={"copy": f["path"]},
            quicklook=f["path"],
            uid=f["path"],
        ))
    emit(items)


# ---------------------------------------------------------------------------
# buuninstall — Uninstall App
# ---------------------------------------------------------------------------


def time_since(ts):
    secs = time.time() - ts
    if secs < 3600:
        return "just now" if secs < 90 else "{} minutes ago".format(max(2, int(round(secs / 60))))
    days = int(secs // 86400)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return "{} days ago".format(days)
    if days < 365:
        return "1 month ago" if days // 30 == 1 else "{} months ago".format(days // 30)
    return "1 year ago" if days // 365 == 1 else "{} years ago".format(days // 365)


def app_sizes_file():
    return os.path.join(CACHE_DIR, "app-sizes.json")


UNUSED_DAYS = 90


def cmd_uninstall(query):
    sizes, last_used = {}, {}
    stale = True
    try:
        with open(app_sizes_file()) as f:
            data = json.load(f)
        if "sizes" in data:
            sizes, last_used = data["sizes"], data.get("last_used", {})
        else:  # older cache format: path -> size
            sizes = data
        stale = time.time() - os.path.getmtime(app_sizes_file()) > APP_SIZES_TTL or "sizes" not in data
    except (OSError, ValueError):
        pass
    if stale:
        job_for("app-sizes").ensure(engine_argv("app-sizes", app_sizes_file()), ttl=60, timeout=600)

    # Special queries: "unused" (not opened for 90+ days) and "big" (largest first)
    if query.startswith("="):
        return uninstall_preview(query[1:].strip(), sizes)
    if query.strip().lower() in ("leftovers", "leftover"):
        return cmd_leftovers()

    words = query.lower().split()
    special = bool(words) and set(words) <= {"unused", "big", "size"}
    unused_only = special and "unused" in words
    by_size = special
    if special:
        query = ""
    cutoff = time.time() - UNUSED_DAYS * 86400
    apps = [a for a in engine.list_apps() if not a["name"].startswith("Alfred ")]  # the app running this
    if unused_only and last_used:
        apps = [a for a in apps if (last_used.get(a["path"]) or 0) < cutoff]
    if by_size:
        apps.sort(key=lambda a: -sizes.get(a["path"], 0))
    items = []
    if not words and not stale:
        unused = [a for a in apps if (last_used.get(a["path"]) or 0) < cutoff]
        if unused:
            items.append(item(
                "{} Not Opened in {}+ Days".format(plural(len(unused), "App"), UNUSED_DAYS),
                "{} · ↩ Show them, biggest first · type “big” to sort everything by size".format(format_bytes(sum(sizes.get(a["path"], 0) for a in unused))),
                icon("hidden"), valid=False, autocomplete="unused", uid="uninstall-unused",
            ))
        items.append(item(
            "Leftovers of Deleted Apps",
            "Settings and caches from apps that are no longer installed · ↩ Find them",
            icon("warning"), valid=False, autocomplete="leftovers", uid="uninstall-leftovers",
        ))
    for app in apps:
        if not matches(query, app["name"]):
            continue
        size = sizes.get(app["path"], 0)
        used = last_used.get(app["path"])
        if used:
            opened = "Opened " + time_since(used)
        elif app["path"] in last_used:
            opened = "No recorded use"
        elif app["mtime"] > 946684800:  # some apps ship with a 1970 date, which says nothing
            opened = "Modified " + time_since(app["mtime"])
        else:
            opened = None
        sub = ([format_bytes(size)] if size else []) + ([opened] if opened else []) + ["↩ Review · ⌥↩ Uninstall"]
        items.append(item(
            app["name"], " · ".join(sub), file_icon(app["path"]),
            valid=False, autocomplete="=" + app["path"],
            mods={
                "alt": mod("Uninstall now (asks to confirm)", act("uninstall", app["path"], name=app["name"], size=size)),
                "cmd": mod("Reveal in Finder", act("reveal", app["path"])),
                "ctrl": mod("Copy path", act("copy", app["path"])),
            },
            text={"copy": app["path"], "largetype": app["path"]},
            uid="app-" + app["path"],
        ))
    emit(items or [item("No matching applications", "", icon("search"), valid=False)], learn=True)


def residuals_for(app_path, name):
    """Leftovers and warnings for an app, cached for 5 minutes (the scan takes a moment)."""
    key = hashlib.sha1(app_path.encode()).hexdigest()[:12]

    def scan():
        ids = engine.app_identifiers(app_path)
        return {
            "residuals": engine.find_residual_files(name, ids, app_path),
            "extensions": engine.system_extensions(ids.get("bundle_id"), engine.app_team_id(app_path)),
            "uninstallers": engine.vendor_uninstallers(app_path, name),
        }
    data = engine.cached("residuals-" + key, 300, scan)
    return data if isinstance(data, dict) else {"residuals": data, "extensions": [], "uninstallers": []}


def excluded_file(app_path):
    return "uninstall-exclude-{}.json".format(hashlib.sha1(app_path.encode()).hexdigest()[:12])


def uninstall_preview(app_path, sizes):
    """Everything that will be removed with an app; ↩ on a leftover leaves it out."""
    if not app_path.endswith(".app") or not os.path.isdir(app_path):
        emit([item("App Not Found", "It may already be uninstalled", icon("check"), valid=False, autocomplete="")])
        return
    name = os.path.basename(app_path)[:-4]
    scan = residuals_for(app_path, name)
    residuals = [r for r in scan["residuals"] if os.path.lexists(r["path"])]
    excluded = set(engine.load_state(excluded_file(app_path)).get("paths", []))
    included = [r for r in residuals if r["path"] not in excluded]
    app_size = sizes.get(app_path, 0) or engine.dir_size(app_path)
    total = app_size + sum(r["size"] for r in included)
    running = engine.app_is_running(app_path)
    locked = any(engine.needs_root(r["path"]) for r in included) or engine.needs_root(app_path)
    data = [r for r in included if is_data(r)]
    items = [
        item("..", "Back to all apps", icon("back"), valid=False, autocomplete=""),
        item(
            "Uninstall {} Completely  —  {}".format(name, format_bytes(total)),
            "{}{}{} · ↩ Move to the Trash".format(
                "Quits it first · " if running else "",
                "app + {}".format(plural(len(included), "leftover")) if residuals else "no leftovers found",
                " · 🔒" if locked else ""),
            file_icon(app_path),
            act("uninstall", app_path, name=name, size=app_size, excluded=sorted(excluded), reviewed=True),
        ),
    ]
    for u in scan["uninstallers"]:
        items.append(item(
            "Use {}".format(os.path.basename(u)[:-4]),
            "Recommended by the developer: it also removes drivers and extensions · ↩ Open",
            file_icon(u), act("open", u),
        ))
    for ext in scan["extensions"]:
        items.append(item(
            "Has a System Extension: {}".format(ext["name"]),
            "macOS keeps it until it's removed in Login Items & Extensions · ↩ Open Settings",
            icon("warning"), act("open", LOGIN_ITEMS_SETTINGS),
        ))
    if data:
        items.append(item(
            "Reset {}  —  {}".format(name, format_bytes(sum(r["size"] for r in data))),
            "Keep the app, remove its settings and data so it starts fresh · ↩ Reset",
            icon("refresh"),
            act("uninstall", app_path, name=name, size=app_size, excluded=sorted(excluded), reviewed=True, reset=True),
        ))
    for r in residuals:
        out = r["path"] in excluded
        items.append(item(
            os.path.basename(r["path"]),
            "{} · {}{} · {}".format(
                format_bytes(r["size"]), r["location"], " 🔒" if engine.needs_root(r["path"]) else "",
                "✗ kept · ↩ Include" if out else "✓ will be removed · ↩ Keep it"),
            icon("hidden" if out else "doc"),
            act("uninstall_toggle", r["path"], app=app_path),
            mods={"cmd": mod("Reveal in Finder", act("reveal", r["path"])), "ctrl": mod("Copy path", act("copy", r["path"]))},
            text={"copy": r["path"], "largetype": r["path"]},
            quicklook=r["path"],
        ))
    emit(items)


def cmd_leftovers():
    groups = engine.cached("orphans", 600, engine.orphaned_leftovers)
    groups = [g for g in groups if any(os.path.lexists(x) for x in g["paths"])]
    items = [item("..", "Back to all apps", icon("back"), valid=False, autocomplete="")]
    if not groups:
        items.append(item("No Leftovers Found", "Nothing from deleted apps that's safe to point at", icon("check"), valid=False))
        emit(items)
        return
    total = sum(g["size"] for g in groups)
    all_paths = [x for g in groups for x in g["paths"]]
    items.append(item(
        "{} Left Files Behind  —  {}".format(plural(len(groups), "Deleted App"), format_bytes(total)),
        "Only apps with nothing installed from the same maker · ↩ Move all to the Trash",
        icon("trash"),
        act("trash_many", "", paths=all_paths, confirm="Remove leftovers of {}?".format(plural(len(groups), "deleted app")),
            message="\n".join("• {} ({})".format(g["bundle_id"], format_bytes(g["size"])) for g in groups[:25]),
            button="Move to Trash", bytes=total, label="Leftovers of deleted apps",
            done="Leftovers of {} moved to the Trash".format(plural(len(groups), "app")), reopen=KEYWORDS["uninstall"] + " leftovers",
            invalidate_memo=["orphans"]),
    ))
    for g in groups:
        where = ", ".join(sorted(set(os.path.basename(os.path.dirname(x)) for x in g["paths"])))
        items.append(item(
            g["bundle_id"],
            "{} · {} · last used {}".format(format_bytes(g["size"]), where, time_since(g.get("last_modified", 0))),
            icon("warning"),
            act("trash_many", "", paths=g["paths"], confirm="Remove {}'s leftovers?".format(g["bundle_id"]),
                message="\n".join("• " + tilde(x) for x in g["paths"]), button="Move to Trash", bytes=g["size"],
                label="Leftovers of " + g["bundle_id"], done="Leftovers of {} moved to the Trash".format(g["bundle_id"]),
                reopen=KEYWORDS["uninstall"] + " leftovers", invalidate_memo=["orphans"]),
            mods={"cmd": mod("Reveal in Finder", act("reveal", g["paths"][0])), "ctrl": mod("Copy paths", act("copy", "\n".join(g["paths"])))},
            text={"copy": "\n".join(g["paths"]), "largetype": "\n".join(tilde(x) for x in g["paths"])},
        ))
    emit(items)


# ---------------------------------------------------------------------------
# buanalyze — Analyze Disk
# ---------------------------------------------------------------------------

MAX_VISIBLE_ENTRIES = 500


def analyze_locations():
    cands = [
        ("Home", HOME), ("Downloads", os.path.join(HOME, "Downloads")), ("Desktop", os.path.join(HOME, "Desktop")),
        ("Documents", os.path.join(HOME, "Documents")), ("Library", os.path.join(HOME, "Library")),
        ("Applications", "/Applications"), ("Startup Disk", "/"),
    ]
    return [(t, p) for t, p in cands if os.path.exists(p)]


def split_analyze_query(query):
    """'~/Downloads/zo' -> ('/Users/me/Downloads', 'zo'). None if not a path."""
    q = query.strip()
    if not (q.startswith("/") or q.startswith("~")):
        return None
    expanded = os.path.expanduser(q)
    if os.path.isdir(expanded):
        return os.path.normpath(expanded), ""
    parent, rest = os.path.split(expanded)
    if parent and os.path.isdir(parent):
        return os.path.normpath(parent), rest
    return None


def dir_autocomplete(path):
    t = tilde(path)
    return t if t.endswith("/") else t + "/"


def cmd_analyze(query):
    target = split_analyze_query(query)
    default = os.path.expanduser(os.environ.get("analyze_path", "").strip())
    if target is None and not query.strip() and default and os.path.isdir(default):
        target = (os.path.normpath(default), "")

    if target is None:
        items = []
        for title, path in analyze_locations():
            if matches(query, title, path):
                items.append(item(
                    title, tilde(path) + " · ↩ Analyze", file_icon(path), valid=False, autocomplete=dir_autocomplete(path),
                    mods={"cmd": mod("Reveal in Finder", act("reveal", path)), "ctrl": mod("Copy path", act("copy", path))},
                    text={"copy": path, "largetype": path}, uid="loc-" + path,
                ))
        items.append(item("Type a path to analyze any folder", "e.g. ~/Library/ or /Volumes/…", icon("info"), valid=False, autocomplete="~/"))
        emit(items)
        return

    dir_path, filt = target
    job = job_for("analyze", dir_path)
    state = job.ensure(engine_argv("analyze", dir_path), ttl=ANALYZE_TTL, timeout=600)
    running = state == "running"
    rescan_vars = rescan("analyze", "analyze", key=dir_path, query=dir_autocomplete(dir_path))
    records = job.records()
    items = []

    if dir_path != "/":
        parent = os.path.dirname(dir_path)
        items.append(item("..", "Up to " + tilde(parent), icon("back"), valid=False, autocomplete=dir_autocomplete(parent)))

    err = next((r for r in records if r.get("type") == "error"), None)
    failure = err["message"] if err else (None if running else job.failure())
    if failure:
        items.append(item("Analysis Failed", failure, icon("error"), rescan_vars, mods={"cmd": mod("Reveal in Finder", act("reveal", dir_path))}))
        emit(items)
        return

    entries = sorted((r for r in records if r.get("type") == "entry"), key=lambda e: -e["size"])
    start = next((r for r in records if r.get("type") == "start"), {})
    total = sum(e["size"] for e in entries)
    if running:
        items.append(item(
            "Scanning {}… {}s".format(tilde(dir_path), elapsed(job)),
            "{} of {} items measured · {} so far".format(len(entries), start.get("count", "?"), format_bytes(total)),
            icon("search"), valid=False,
        ))
    else:
        items.append(item(
            "{}  —  {}".format(tilde(dir_path), format_bytes(total)),
            "{} · ↩ Reveal in Finder · ⌃↩ Rescan".format(plural(len(entries), "item")),
            icon("analyze"), act("reveal", dir_path),
            mods={"ctrl": mod("Rescan this folder", rescan_vars)}, text={"copy": dir_path},
        ))

    visible = [e for e in entries if matches(filt, e["name"])]
    for e in visible[:MAX_VISIBLE_ENTRIES]:
        ratio = e["size"] / total if total else 0
        color = "red" if ratio > 0.3 else "orange" if ratio > 0.1 else "yellow" if ratio > 0.05 else "gray"
        trash = act(
            "trash", e["path"], confirm="Move {} to Trash?".format(e["name"]),
            message="{} ({}) will be moved to the Trash.".format(tilde(e["path"]), format_bytes(e["size"])),
            button="Move to Trash", bytes=e["size"], done="{} moved to the Trash".format(e["name"]),
            invalidate_analyze=[dir_path], reopen=KEYWORDS["analyze"] + " " + dir_autocomplete(dir_path),
        )
        it = item(
            e["name"],
            "{} · {} · {:.1f}%".format("Folder" if e["is_dir"] else "File", format_bytes(e["size"]), ratio * 100),
            icon("size-" + color),
            mods={
                "cmd": mod("Reveal in Finder", act("reveal", e["path"])),
                "alt": mod("Move to Trash", trash),
                "ctrl": mod("Copy path", act("copy", e["path"])),
            },
            text={"copy": e["path"], "largetype": e["path"]},
            quicklook=e["path"],
        )
        if e["is_dir"]:
            it["valid"] = False
            it["autocomplete"] = dir_autocomplete(e["path"])
            it["subtitle"] += " · ↩ Open"
        else:
            it["variables"] = act("reveal", e["path"])
            it["arg"] = e["path"]
            it["subtitle"] += " · ↩ Reveal"
        items.append(it)
    hidden = len(visible) - MAX_VISIBLE_ENTRIES
    if hidden > 0:
        items.append(item("{} smaller items hidden".format(hidden), "Type after the / to filter", icon("hidden"), valid=False))
    emit(items, rerun=0.5 if running else None)


# ---------------------------------------------------------------------------
# bularge — Large Files
# ---------------------------------------------------------------------------


def large_threshold():
    try:
        return max(50, int(os.environ.get("large_min_mb", "500") or 500)) * 1024 * 1024
    except ValueError:
        return 500 * 1024 * 1024


def size_label(nbytes):
    mb = nbytes / 1048576
    return "{:g} GB".format(round(mb / 1024, 1)) if mb >= 1024 else "{:g} MB".format(round(mb))


LARGE_KINDS = {
    "video": (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".wmv", ".flv", ".mpg", ".mpeg"),
    "audio": (".mp3", ".wav", ".aiff", ".aif", ".flac", ".m4a", ".aac", ".ogg"),
    "image": (".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".raw", ".psd", ".dng", ".cr2", ".nef"),
    "archive": (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"),
    "disk": (".dmg", ".iso", ".img", ".vmdk", ".qcow2", ".vdi", ".sparseimage"),
}


def parse_large_query(query):
    """"2gb video old report" -> (min_bytes or None, kind or None, old?, remaining text)"""
    import re
    min_bytes, kind, old, rest = None, None, False, []
    for word in query.lower().split():
        m = re.match(r"^(\d+(?:\.\d+)?)(mb|gb|m|g)$", word)
        if m:
            min_bytes = float(m.group(1)) * (1024 ** 3 if m.group(2).startswith("g") else 1024 ** 2)
        elif word in LARGE_KINDS or word.rstrip("s") in LARGE_KINDS:
            kind = word.rstrip("s")
        elif word == "old":
            old = True
        else:
            rest.append(word)
    return min_bytes, kind, old, " ".join(rest)


def cmd_large(query):
    size_filter, kind, old, query = parse_large_query(query)
    min_bytes = int(size_filter or large_threshold())
    files = engine.large_files(min_bytes)
    if kind:
        files = [f for f in files if f["name"].lower().endswith(LARGE_KINDS[kind])]
    if old:
        cutoff = time.time() - 180 * 86400
        files = [f for f in files if (f.get("last_used") or f["mtime"]) < cutoff]
    if not files:
        emit([item("No Files Larger Than {}".format(size_label(min_bytes)), "Spotlight found nothing that big in your home folder", icon("check"), valid=False)])
        return
    total = sum(f["size"] for f in files)
    items = [item(
        "{}{} Larger Than {}  —  {}".format(plural(len(files), "File"), " ({})".format(kind) if kind else "", size_label(min_bytes), format_bytes(total)),
        "Filter: 2gb · video · audio · image · archive · disk · old · {} free".format(format_bytes(disk_free())),
        icon("large"), valid=False,
    )]
    for f in files:
        folder = tilde(os.path.dirname(f["path"]))
        if not matches(query, f["name"], folder):
            continue
        items.append(item(
            f["name"],
            "{} · {} · {}".format(
                format_bytes(f["size"]),
                "opened " + time_since(f["last_used"]) if f.get("last_used") else "modified " + time_since(f["mtime"]),
                folder),
            file_icon(f["path"]),
            act("open", f["path"]),
            mods={
                "alt": mod("Move to Trash", act(
                    "trash", f["path"], confirm="Move {} to Trash?".format(f["name"]),
                    message="{} ({}) will be moved to the Trash.".format(tilde(f["path"]), format_bytes(f["size"])),
                    button="Move to Trash", bytes=f["size"], done="{} moved to the Trash".format(f["name"]), reopen=KEYWORDS["large"] + " ")),
                "cmd": mod("Reveal in Finder", act("reveal", f["path"])),
                "ctrl": mod("Copy path", act("copy", f["path"])),
            },
            text={"copy": f["path"], "largetype": f["path"]},
            quicklook=f["path"],
            uid="large-" + f["path"],
        ))
    emit(items)


# ---------------------------------------------------------------------------
# bustartup — Startup Items
# ---------------------------------------------------------------------------

LOGIN_ITEMS_SETTINGS = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


def cmd_startup(query):
    entries = engine.startup_items()
    orphans = [e for e in entries if e["missing"]]
    items = []
    if orphans:
        items.append(item(
            "Remove {} Left Behind by Deleted Apps".format(plural(len(orphans), "Startup Item")),
            "Their programs no longer exist · ↩ Move them to the Trash",
            icon("warning"),
            act("startup_remove", "", entries=orphans),
        ))
    items.append(item("Open Login Items Settings", "Apps that open at login and background permissions live in System Settings", icon("startup"), act("open", LOGIN_ITEMS_SETTINGS)))
    for e in sorted(entries, key=lambda e: (not e["missing"], (e["owner"] or e["label"]).lower())):
        if not matches(query, e["label"], e["owner"], e["kind"], e["program"]):
            continue
        state = "Program missing" if e["missing"] else "Disabled" if e["disabled"] else ("Running" if e["loaded"] else "Not running") if e["loaded"] is not None else "Enabled"
        items.append(item(
            e["owner"] or e["label"],
            " · ".join(x for x in (e["kind"], state, e["label"] if e["owner"] else "", "🔒" if e["system"] else "") if x),
            icon("warning" if e["missing"] else "startup"),
            act("reveal", e["path"]),
            mods={
                "alt": mod("Remove this startup item", act("startup_remove", "", entries=[e])),
                "ctrl": mod("Copy path", act("copy", e["path"])),
                "cmd": mod("Reveal the program" if e["program"] and not e["missing"] else "Reveal the plist", act("reveal", e["program"] if e["program"] and not e["missing"] else e["path"])),
            },
            text={"copy": e["path"], "largetype": "{}\n{}".format(e["label"], e["program"])},
            uid="startup-" + e["path"],
        ))
    emit(items)


# ---------------------------------------------------------------------------
# bukill — Processes
# ---------------------------------------------------------------------------

PROCESS_VIEW = "process-view.json"
PROCESS_ROWS = 60


def process_view():
    view = engine.load_state(PROCESS_VIEW)
    return {"sort": view.get("sort") if view.get("sort") in ("cpu", "memory") else "cpu", "group": view.get("group", True)}


def cmd_processes(query):
    view = process_view()
    procs = engine.list_processes()
    if view["group"]:
        rows = engine.group_processes(procs)
    else:
        rows = [{"name": p["name"], "app": engine.app_bundle_of(p["path"]), "pid": p["pid"], "pids": [p["pid"]], "path": p["path"],
                 "uid": p["uid"], "others": p["uid"] != os.getuid(), "cpu": p["cpu"], "mem": p["mem"],
                 "critical": p["name"] in engine.CRITICAL_PROCESSES or p["pid"] <= 1} for p in procs]
    key = (lambda r: (-r["mem"], -r["cpu"])) if view["sort"] == "memory" else (lambda r: (-r["cpu"], -r["mem"]))
    rows.sort(key=key)
    q = query.strip()
    items = []
    if not q:
        other = "memory" if view["sort"] == "cpu" else "CPU"
        items.append(item(
            "{}  —  sorted by {}".format(plural(len(procs), "Process"), "CPU" if view["sort"] == "cpu" else "memory"),
            "↩ Sort by {} · ⌥↩ {}".format(other, "Show every process separately" if view["group"] else "Group app helpers"),
            icon("process"), act("proc_view", "", sort="memory" if view["sort"] == "cpu" else "cpu"),
            mods={
                "alt": mod("Show every process separately" if view["group"] else "Group each app's helper processes under it",
                           act("proc_view", "", group=not view["group"])),
                "cmd": mod("Open Activity Monitor", act("open_app", "Activity Monitor")),
            },
            uid="processes-summary",
        ))
    shown = 0
    for r in rows:
        if q.isdigit():
            if not any(str(pid).startswith(q) for pid in r["pids"]):
                continue
        elif "/" in q:
            if q.lower() not in r["path"].lower():
                continue
        elif not matches(q, r["name"]):
            continue
        shown += 1
        if shown > PROCESS_ROWS:
            break
        payload = {"pids": r["pids"], "pid": r["pid"], "path": r["path"], "app": r["app"], "name": r["name"], "critical": r["critical"]}
        details = ["CPU " + format_percent(r["cpu"]), format_bytes(r["mem"])]
        if len(r["pids"]) > 1:
            details.append(plural(len(r["pids"]), "process"))
        if r["others"]:
            details.append("🔒 " + ("root" if r["uid"] == 0 else "another user"))
        if r["critical"]:
            details.append("⚠️ system")
        verb = "Quit" if r["app"] else "End"
        mods = {
            "alt": mod("Force quit — unsaved changes are lost" + (" · 🔒 asks for your password" if r["others"] else ""),
                       act("proc_quit", r["name"], force=True, **payload)),
            "cmd": mod("Reveal in Finder", act("reveal", r["path"])),
            "ctrl": mod("Copy path", act("copy", r["path"])),
        }
        if r["app"] and not r["critical"]:
            mods["fn"] = mod("Restart {} (quit it and open it again)".format(r["name"]), act("proc_restart", r["name"], **payload))
        items.append(item(
            r["name"], " · ".join(details + ["↩ " + verb, "⌥↩ Force quit"]),
            file_icon(r["app"]) if r["app"] else icon("warning" if r["critical"] else "process"),
            act("proc_quit", r["name"], force=False, **payload), mods=mods,
            text={"copy": r["path"], "largetype": "{}\nPID {}\n{}".format(r["name"], ", ".join(str(p) for p in r["pids"][:40]), r["path"])},
            quicklook=None,
        ))
    if q and not shown:
        items.append(item("No Matching Process", "Type part of a name, a PID, or a path with /", icon("search"), valid=False))
    emit(items)


def proc_quit(p, force):
    name = p.get("name") or "the process"
    if p.get("critical") and not confirm(
            "{} {}?".format("Force quit" if force else "End", name),
            "{} is part of macOS. Ending it can log you out, restart the Mac or make it stop responding.".format(name), "End Anyway"):
        return None
    if force and os.environ.get("confirm_force", "1") not in ("0", "false") and not p.get("critical") and not confirm(
            "Force Quit {}?".format(name), "Unsaved changes in {} will be lost.".format(name), "Force Quit"):
        return None
    pids = [pid for pid in p.get("pids", []) if engine.process_matches(pid, p["path"])]
    if not pids:
        return "{} isn't running any more".format(name)
    if p.get("app") and not force:
        bid = engine.read_plist_key(engine.app_info_plist(p["app"]), "CFBundleIdentifier")
        if engine.quit_app(p["app"], bid, wait=8):
            return "Quit " + name
        return "{} is still open (it may be asking to save) · ⌥↩ to force quit".format(name)
    alive, cancelled = engine.signal_processes(pids, signal.SIGKILL if force else signal.SIGTERM,
                                               "Burrow wants to {} {}.".format("force quit" if force else "end", name))
    if cancelled:
        return None
    if alive:
        return "{} is still running{}".format(name, "" if force else " · ⌥↩ to force quit")
    return "{} {}".format("Force quit" if force else "Ended", name)


def proc_restart(p):
    name = p.get("name") or "the app"
    if not p.get("app") or not engine.process_matches(p["pid"], p["path"]):
        return "{} isn't running any more".format(name)
    bid = engine.read_plist_key(engine.app_info_plist(p["app"]), "CFBundleIdentifier")
    if not engine.quit_app(p["app"], bid, wait=10):
        return "{} didn't quit (it may be asking to save), so it wasn't restarted".format(name)
    subprocess.run(["/usr/bin/open", "-a", p["app"]])
    return "Restarted " + name


# ---------------------------------------------------------------------------
# budupes — Duplicate Files
# ---------------------------------------------------------------------------


def cmd_dupes(query):
    """Queries: "" or text = default folders (filtered); "~/path" = offer to scan
    that folder; "@~/path" = scan it; "=ID" = one duplicated file's copies."""
    group_id = None
    scope = ""
    q = query.strip()
    if q.startswith(("/", "~")):
        # Typing a path only offers the scan, so each keystroke doesn't start one.
        target = split_analyze_query(q)
        items = []
        if target and not target[1]:
            items.append(item(
                "Search {} for Duplicates".format(tilde(target[0])), "↩ Start the search", icon("dupes"),
                valid=False, autocomplete="@" + dir_autocomplete(target[0]),
            ))
        elif target:
            parent, partial = target
            try:
                names = os.listdir(parent)
            except OSError:
                names = []
            for name in sorted(n for n in names if n.lower().startswith(partial.lower()) and not n.startswith("."))[:20]:
                full = os.path.join(parent, name)
                if os.path.isdir(full):
                    items.append(item(name, tilde(full), file_icon(full), valid=False, autocomplete=dir_autocomplete(full)))
        emit(items or [item("Type a folder path", "e.g. ~/Pictures/", icon("info"), valid=False)])
        return
    if q.startswith("="):
        group_id = q[1:].split()[0] if q[1:].split() else ""
        roots = engine.load_state("dupes-last.json").get("roots") or engine.default_dupe_roots()
        scope = engine.load_state("dupes-last.json").get("scope", "")
        query = ""
    else:
        if q.startswith("@") and "/" in q:
            # "@~/My Stuff/ filter": the folder ends at the last "/" (paths can contain spaces)
            end = q.rindex("/") + 1
            scope = q[:end]
            path = os.path.expanduser(scope[1:].rstrip("/") or "/")
            roots = [os.path.normpath(path)] if os.path.isdir(path) else engine.default_dupe_roots()
            query = q[end:].strip()
        else:
            roots = engine.default_dupe_roots()
        last = engine.load_state("dupes-last.json")
        if last.get("roots") != roots or last.get("scope") != scope:
            engine.save_state("dupes-last.json", {"roots": roots, "scope": scope})
    key = "|".join(roots)
    job = job_for("dupes", key)
    state = job.ensure(engine_argv("dupes", *roots), ttl=30 * 60, timeout=1800)
    running = state == "running"
    records = job.records()
    groups = []
    for g in records:
        if g.get("type") != "group":
            continue
        paths = [x for x in g["paths"] if os.path.lexists(x)]
        if len(paths) > 1:
            groups.append(dict(g, paths=paths))
    rescan_vars = rescan("dupes", "dupes", key=key, query=scope)

    if group_id is not None:
        g = next((g for g in groups if g["id"] == group_id), None)
        items = [item("..", "Back to all duplicates", icon("back"), valid=False, autocomplete=scope)]
        if not g:
            items.append(item("Only One Copy Left", "This file has no duplicates any more", icon("check"), valid=False))
            emit(items)
            return
        newest = max(g["paths"], key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0)
        for path in g["paths"]:
            items.append(item(
                os.path.basename(path),
                "{} · modified {}{} · ↩ Reveal · ⌥↩ Trash this copy".format(
                    tilde(os.path.dirname(path)), time_since(os.path.getmtime(path)), " · newest" if path == newest else ""),
                file_icon(path), act("reveal", path),
                mods={"alt": mod("Move this copy to the Trash", act(
                    "dupes_trash", path, trash=[path], keep=[x for x in g["paths"] if x != path], size=g["size"],
                    confirm="Trash this copy?",
                    message="{} will be moved to the Trash. {} other {} stay.".format(tilde(path), len(g["paths"]) - 1, "copy will" if len(g["paths"]) == 2 else "copies"),
                    button="Move to Trash", label="Duplicate " + os.path.basename(path),
                    reopen=KEYWORDS["dupes"] + " =" + g["id"]))},
                quicklook=path, text={"copy": path, "largetype": path},
            ))
        emit(items)
        return

    groups.sort(key=lambda g: -g["size"] * (len(g["paths"]) - 1))
    wasted = sum(g["size"] * (len(g["paths"]) - 1) for g in groups)
    progress = next((r for r in reversed(records) if r.get("type") == "progress"), {})
    where = ", ".join(tilde(r) for r in roots[:3]) + ("…" if len(roots) > 3 else "")
    items = []
    if running:
        stage = "Comparing contents" if progress.get("hashing") else "Looking at {} files".format(progress.get("files", 0))
        items.append(item("Scanning for Duplicates… {}s".format(elapsed(job)), "{} in {}".format(stage, where), icon("search"), valid=False))
    elif not groups:
        items.append(item("No Duplicates Found", "No identical files over 1 MB in {} · type a path to search elsewhere".format(where), icon("check"), rescan_vars))
        emit(items)
        return
    else:
        plan = []
        for g in groups:
            newest = max(g["paths"], key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0)
            plan.append({"keep": newest, "trash": [x for x in g["paths"] if x != newest], "size": g["size"]})
        items.append(item(
            "{} Wasted by Duplicates".format(format_bytes(wasted)),
            "{} in {} · ⌥↩ Keep the newest copy of each".format(plural(len(groups), "duplicated file"), where),
            icon("dupes"), valid=False,
            mods={"alt": mod("Keep the newest copy of every file, trash the rest ({})".format(format_bytes(wasted)), act(
                "dupes_trash_all", "", groups=plan, reopen=KEYWORDS["dupes"] + " " + scope))},
        ))
    for g in groups:
        name = os.path.basename(g["paths"][0])
        if not matches(query, name, *g["paths"]):
            continue
        extra = g["paths"][1:]
        newest = max(g["paths"], key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0)
        others = [x for x in g["paths"] if x != newest]
        items.append(item(
            name,
            "{} copies · {} each · {} wasted · ↩ Choose · ⌥↩ Keep newest".format(len(g["paths"]), format_bytes(g["size"]), format_bytes(g["size"] * len(extra))),
            file_icon(g["paths"][0]), valid=False, autocomplete="=" + g["id"],
            mods={"alt": mod("Keep the newest copy and trash the other {}".format(len(others)), act(
                "dupes_trash", "", trash=others, keep=[newest], size=g["size"], confirm="Keep the newest copy of {}?".format(name),
                message="Keeps {}\n\nMoves to the Trash:\n{}".format(tilde(newest), "\n".join("• " + tilde(o) for o in others)),
                button="Trash {}".format(plural(len(others), "copy") if len(others) > 1 else "1 copy"),
                label="Duplicates of " + name, reopen=KEYWORDS["dupes"] + " " + scope))},
            quicklook=g["paths"][0], uid="dupe-" + g["id"],
        ))
    emit(items, rerun=0.5 if running else None)


# ---------------------------------------------------------------------------
# buupdates — App Updates
# ---------------------------------------------------------------------------

UPDATE_TTL = 6 * 3600


def cmd_updates(query):
    import updates
    job = job_for("updates")
    state = job.ensure(engine_argv("updates"), ttl=UPDATE_TTL, timeout=600)
    running = state == "running"
    result = engine.load_state(updates.RESULTS)
    items = []
    if running and not result:
        emit([item("Checking Your Apps for Updates… {}s".format(elapsed(job)),
                   "App Store, Homebrew, Sparkle and Electron feeds", icon("search"), valid=False)], rerun=0.5)
        return
    failure = None if running else job.failure()
    ups = updates.visible_updates(result)
    ignored = engine.load_state(updates.IGNORE)
    installable = [u for u in ups if u.get("installable")]
    checked_ago = time_since(result["time"]) if result.get("time") else "never"

    if running:
        items.append(item("Checking for Updates… {}s".format(elapsed(job)), "Showing the last results meanwhile", icon("search"), valid=False))
    win = window_item("updates", "Open the Updates Window", "Release notes, update all, roll back, with progress for each app")
    if win and not query:
        items.append(win)
    if failure and result and ups:
        items.append(item("The Last Check Didn't Finish", "{} · showing results from {} · ↩ Try again".format(failure, checked_ago),
                          icon("warning"), act("rescan", KEYWORDS["updates"] + " ", kind="updates")))
    if ups and not query:
        items.append(item(
            "{} Available".format(plural(len(ups), "Update")),
            ("↩ Install {} · checked {}".format(
                ("all " + plural(len(installable), "update") + " Burrow can install") if len(installable) > 1
                else "the one Burrow can install", checked_ago) if installable
             else "Install {} from the rows below · checked {}".format("it" if len(ups) == 1 else "them", checked_ago)),
            icon("update"),
            act("update_install", "", paths=[u["path"] for u in installable]) if installable else None,
            valid=bool(installable),
        ))
    elif not running:
        items.append(item(
            "Everything's Up to Date" if not failure else "Update Check Failed",
            (failure + " · ↩ Try again") if failure else "{} apps checked {} · ↩ Check again".format(result.get("checked", 0), checked_ago),
            icon("check" if not failure else "error"), act("rescan", KEYWORDS["updates"] + " ", kind="updates"),
        ))

    store_waiting = [u for u in ups if u["source"] == "App Store" and not u.get("installable") and not u.get("ios")]
    if store_waiting and not query:
        items.append(item(
            "Update App Store Apps From Burrow Too",
            "Install the free mas tool: brew install mas · ↩ Copy the command",
            icon("info"), act("copy", updates.MAS_INSTALL_COMMAND),
            mods={"cmd": mod("About mas (github.com/mas-cli/mas)", act("open", "https://github.com/mas-cli/mas"))},
        ))
    for u in ups:
        if not matches(query, u["name"], u["source"]):
            continue
        if u["source"] == "App Store" and u.get("installable"):
            how = "↩ Update (asks for your password)"
            action = act("update_install", "", paths=[u["path"]])
        elif u["source"] == "App Store":
            how = "↩ Open in the App Store" + (" (iPhone/iPad app)" if u.get("ios") else "")
            action = act("open", u["url"])
        elif u.get("installable"):
            how = "↩ Update" if u["source"] != "Homebrew" else "↩ Update with Homebrew"
            action = act("update_install", "", paths=[u["path"]])
        else:
            how = "↩ Open the developer's site"
            action = act("open", u.get("notes") or u.get("url") or "")
        items.append(item(
            "{}  {} → {}".format(u["name"], u["installed"], u["version"]),
            "{} · {}".format(u["source"], how),
            file_icon(u["path"]), action,
            mods={
                "cmd": mod("Release notes / website", act("open", u.get("notes") or u.get("url") or ""), valid=bool(u.get("notes") or u.get("url"))),
                "alt": mod("Skip version {}".format(u["version"]), act("update_ignore", u["bundle_id"], rule=u["version"], name=u["name"])),
                "ctrl": mod("Never check {}".format(u["name"]), act("update_ignore", u["bundle_id"], rule="*", name=u["name"])),
            },
            text={"largetype": u.get("release_notes") or "{} {} → {}".format(u["name"], u["installed"], u["version"]),
                  "copy": "{} {}".format(u["name"], u["version"])},
            uid="update-" + u["path"],
        ))

    if query and ups and not any(matches(query, u["name"], u["source"]) for u in ups):
        items.append(item("No Updates Match “{}”".format(query), "{} available in total".format(plural(len(ups), "update")), icon("search"), valid=False))
    if not running:
        if ups:
            items.append(item("Check Again", "Last checked {} · {} apps up to date".format(checked_ago, result.get("current", 0)),
                              icon("refresh"), act("rescan", KEYWORDS["updates"] + " ", kind="updates")))
        if ignored:
            items.append(item(
                "{} Skipped or Ignored".format(plural(len(ignored), "App")), "↩ Show all updates again · ⌘L to see which",
                icon("hidden"), act("update_unignore"),
                text={"largetype": "\n".join("{}: {}".format(k, "always" if v == "*" else "version " + v) for k, v in sorted(ignored.items()))},
            ))
        mode = os.environ.get("auto_updates", "off")
        items.append(item(
            "Automatic Checks: {}".format({"off": "Off", "notify": "Notify Daily", "install": "Install Daily"}.get(mode, mode)),
            "Change it in the Workflow’s Configuration · {} apps have no update source".format(result.get("unknown", 0)),
            icon("info"), valid=False,
        ))
    emit(items, rerun=0.5 if running else None)


def window_item(section, title, subtitle):
    """A row that opens Burrow Companion's window, when the optional app is installed."""
    if not companion_path():
        return None
    return item(title, subtitle, icon("menubar"), act("companion", section))


def install_updates(paths):
    import updates
    result = engine.load_state(updates.RESULTS)
    todo = [u for u in updates.visible_updates(result) if u["path"] in paths and u.get("installable")]
    if not todo:
        return "Nothing to install"
    names = "\n".join("• {} {} → {} ({})".format(u["name"], u["installed"], u["version"], u["source"]) for u in todo)
    running = [u["name"] for u in todo if engine.app_is_running(u["path"])]
    msg = "Burrow checks every download (same app, same developer, valid signature) before installing it. The old version goes to the Trash, so you can undo.\n\n" + names
    if any(u["source"] == "App Store" for u in todo):
        msg += "\n\nApp Store updates come from Apple through your signed-in Apple Account, and ask for your password once."
    if running:
        msg += "\n\n{} will be quit and reopened.".format(", ".join(running))
    if not confirm("Install {}?".format(plural(len(todo), "update")), msg, "Install"):
        return None
    done, failed = [], []
    store = [u for u in todo if u["source"] == "App Store"]
    if store:  # all App Store updates share one password prompt
        try:
            d, f = updates.install_app_store(store)
            done += d
            failed += f
        except Exception as e:  # noqa: BLE001
            failed += ["{}: {}".format(u["name"], e) for u in store]
    for u in [u for u in todo if u["source"] != "App Store"]:
        try:
            done.append(updates.install(u, log=notify))
        except Exception as e:  # noqa: BLE001
            failed.append("{}: {}".format(u["name"], e))
    job_for("updates").clear()
    alfred_search(KEYWORDS["updates"] + " ")
    parts = []
    if done:
        parts.append(done[0] if len(done) == 1 else "Updated {}".format(plural(len(done), "app")))
    if failed:
        parts.append("Not updated — " + "; ".join(failed[:3]))
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# bubrowsers — Browser cleaning and reset
# ---------------------------------------------------------------------------

BROWSER_CHOICES = "browser-choices.json"
DEFAULT_BROWSER_CATEGORIES = ["cache", "history", "downloads"]
FULL_DISK_ACCESS = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def browser_choice(bid):
    import browsers
    c = engine.load_state(BROWSER_CHOICES).get(bid) or {}
    cats = [k for k in c.get("categories", DEFAULT_BROWSER_CATEGORIES) if k in browsers.CATEGORY_KEYS]
    return {"categories": cats, "range": c.get("range", "all"), "profile": c.get("profile", "all")}


def set_browser_choice(bid, **changes):
    def change(d):
        cur = d.get(bid) or {}
        cur.update(changes)
        d[bid] = cur
        return d
    engine.update_state(BROWSER_CHOICES, change)


def find_browser(bid):
    import browsers
    return next((b for b in browsers.discover() if b["id"] == bid), None)


def cmd_browsers(query):
    import browsers
    if query.startswith("="):
        return browser_view(query[1:].strip())
    found = browsers.discover()
    items = []
    if not found:
        emit([item("No Browsers Found", "Burrow looks for Chromium, Firefox, Safari and Orion data", icon("check"), valid=False)])
        return
    executables = engine.running_executables()
    running = {b["id"]: browsers.is_running(b, executables) for b in found}
    closed = [b for b in found if not running[b["id"]] and (b["kind"] != "safari" or b.get("access"))]
    win = window_item("browsers", "Open the Browsers Window", "Choose what to clean with switches, for every browser")
    if win and not query:
        items.append(win)
    items.append(item(
        "Clear the Cache of Every Closed Browser",
        "{} · open browsers are skipped · ↩ Move to the Trash".format(", ".join(b["name"] for b in closed[:5]) or "none are closed"),
        icon("clean"), act("browser_cache_all"), valid=bool(closed),
    ))
    for b in found:
        if not matches(query, b["name"]):
            continue
        state = []
        if not b["installed"]:
            state.append("app not installed")
        if running[b["id"]]:
            state.append("open")
        if b["kind"] == "safari" and not b.get("access"):
            items.append(item(
                "Safari", "Needs Full Disk Access for Alfred · ↩ Open Privacy settings, then turn on Alfred",
                file_icon(b["app"]), act("open", FULL_DISK_ACCESS),
            ))
            continue
        cache = browsers.sizes(b)["cache"]
        items.append(item(
            b["name"],
            " · ".join(x for x in [plural(len(b["profiles"]), "profile"), "cache " + format_bytes(cache) if cache else "", ", ".join(state),
                                   "↩ Choose what to clean"] if x),
            file_icon(b["app"]) if b.get("app") else icon("browser"),
            valid=False, autocomplete="=" + b["id"],
            mods={
                "cmd": mod("Reveal its data folder", act("reveal", b["root"])),
                "alt": mod("Clean now with your saved choices", act("browser_clean", b["id"])),
                "ctrl": mod("Copy the data folder path", act("copy", b["root"])),
            },
            uid="browser-" + b["id"],
        ))
    emit(items)


def browser_view(bid):
    import browsers
    b = find_browser(bid)
    items = [item("..", "All browsers", icon("back"), valid=False, autocomplete="")]
    if not b:
        emit(items + [item("Browser Not Found", "Its data may have been removed", icon("check"), valid=False)])
        return
    choice = browser_choice(bid)
    ranges = dict((k, l) for k, l, _ in browsers.RANGES)
    profiles = b["profiles"]
    prof_ids = [p["id"] for p in profiles] if choice["profile"] == "all" else [choice["profile"]]
    prof_label = "All profiles" if choice["profile"] == "all" or len(profiles) == 1 else next((p["name"] for p in profiles if p["id"] == choice["profile"]), "All profiles")
    running = browsers.is_running(b)
    labels = dict((k, l) for k, l, _, _ in browsers.CATEGORIES)
    try:
        preview = browsers.plan(b, prof_ids, choice["categories"], choice["range"])
    except browsers.BrowserError as e:
        if "profile" in str(e):
            set_browser_choice(bid, profile="all")  # the saved profile is gone: back to all
            return browser_view(bid)
        emit(items + [item(str(e).split(".")[0], "↩ Open Privacy settings", icon("warning"), act("open", FULL_DISK_ACCESS))])
        return
    chosen = ", ".join(labels[c].lower() for c in choice["categories"]) or "nothing selected"
    items.append(item(
        "Clean {}  —  {}".format(b["name"], ranges[choice["range"]].lower()),
        "{}{} · ↩ Clean".format("Quits it first · " if running else "", chosen),
        file_icon(b["app"]) if b.get("app") else icon("browser"),
        act("browser_clean", bid), valid=bool(choice["categories"]),
        text={"largetype": "\n".join(preview["notes"]) or chosen},
    ))
    for key, label, ranged, desc in browsers.CATEGORIES:
        on = key in choice["categories"]
        items.append(item(
            ("✓ " if on else "○ ") + label,
            desc + (" · asks again before removing" if key == "passwords" else "") + " · ↩ " + ("Leave out" if on else "Include"),
            icon("check" if on else "hidden"), act("browser_toggle", bid, category=key),
        ))
    order = [k for k, _, _ in browsers.RANGES]
    nxt = order[(order.index(choice["range"]) + 1) % len(order)]
    prev = order[(order.index(choice["range"]) - 1) % len(order)]
    items.append(item("Time Range: " + ranges[choice["range"]],
                      "↩ “{}” · ⌥↩ “{}” · cache and tabs are always cleared fully".format(ranges[nxt], ranges[prev]), icon("refresh"),
                      act("browser_choice", bid, range=nxt), mods={"alt": mod("Change to “{}”".format(ranges[prev]), act("browser_choice", bid, range=prev))}))
    if len(profiles) > 1:
        ids = ["all"] + [p["id"] for p in profiles]
        nprof = ids[(ids.index(choice["profile"]) + 1) % len(ids)] if choice["profile"] in ids else "all"
        nlabel = "All profiles" if nprof == "all" else next(p["name"] for p in profiles if p["id"] == nprof)
        items.append(item("Profiles: " + prof_label, "↩ Change to “{}”".format(nlabel), icon("info"), act("browser_choice", bid, profile=nprof)))
    for n in preview["notes"]:
        items.append(item(n, "", icon("info"), valid=False))
    if b["kind"] in ("chromium", "firefox"):
        what = ("extensions and their data go to the Trash too" if b["kind"] == "chromium" else "extensions stay")
        items.append(item("Reset Settings", "Defaults; {} · bookmarks, history, passwords stay".format(what),
                          icon("refresh"), act("browser_reset", bid, full=False)))
    if b["kind"] in ("chromium", "firefox", "orion"):
        which = "profile" if len(prof_ids) == 1 else "all {} profiles".format(len(prof_ids))
        items.append(item("Full Reset", "Everything in the {} goes to the Trash, bookmarks and passwords too".format(which),
                          icon("warning"), act("browser_reset", bid, full=True)))
    emit(items)


def quit_browser_if_needed(b, why):
    import browsers
    if not browsers.is_running(b):
        return True
    if not confirm("Quit {}?".format(b["name"]), "{} needs {} to be closed. Open tabs are restored next time unless you're also clearing sessions.".format(why, b["name"]), "Quit"):
        return False
    return engine.quit_app(b["app"], b.get("bundle_id"), wait=15)


def browser_clean_action(bid):
    import browsers
    b = find_browser(bid)
    if not b:
        return "Browser not found"
    choice = browser_choice(bid)
    ranges = dict((k, l) for k, l, _ in browsers.RANGES)
    labels = dict((k, l) for k, l, _, _ in browsers.CATEGORIES)
    prof_ids = [p["id"] for p in b["profiles"]] if choice["profile"] == "all" else [choice["profile"]]
    p = browsers.plan(b, prof_ids, choice["categories"], choice["range"])
    listing = "\n".join("• " + labels[c] for c in choice["categories"])
    msg = "From {} ({}):\n\n{}\n\nEverything goes to the Trash first, so you can undo.".format(
        b["name"], ranges[choice["range"]].lower(), listing)
    if p["notes"]:
        msg += "\n\n" + "\n".join(p["notes"])
    if not confirm("Clean {}?".format(b["name"]), msg, "Clean"):
        return None
    if "passwords" in choice["categories"] and not confirm(
            "Remove saved passwords?", "Every password saved in {} ({}) will be removed. Make sure you can sign in without them, or have them in another password manager.".format(
                b["name"], "all profiles" if choice["profile"] == "all" else "this profile"), "Remove Passwords"):
        return None
    if not quit_browser_if_needed(b, "Cleaning"):
        return "{} is still open, so nothing was cleaned".format(b["name"])
    try:
        res = browsers.clean(b, prof_ids, choice["categories"], choice["range"])
    except Exception as e:  # noqa: BLE001
        return "Couldn't clean {}: {}".format(b["name"], e)
    record_freed(res["freed"])
    alfred_search(KEYWORDS["browsers"] + " =" + bid)
    out = "{} cleaned".format(b["name"])
    if res["freed"]:
        out += " · {} moved to the Trash".format(format_bytes(res["freed"]))
    if res["failed"]:
        out += " · {} couldn't be moved".format(len(res["failed"]))
    return out + " · undo with " + KEYWORDS["hub"]


def browser_reset_action(bid, full):
    import browsers
    b = find_browser(bid)
    if not b:
        return "Browser not found"
    choice = browser_choice(bid)
    prof_ids = [p["id"] for p in b["profiles"]] if choice["profile"] == "all" else [choice["profile"]]
    if full:
        title = "Fully reset {}?".format(b["name"])
        msg = ("Everything in {} goes to the Trash: bookmarks, history, passwords, extensions, cookies and settings. "
               "It starts like a fresh install. If you sync, your data comes back when you sign in.\n\nYou can undo this with Undo in Burrow.").format(
                   "the selected profile" if len(prof_ids) == 1 and len(b["profiles"]) > 1 else b["name"])
    else:
        title = "Reset {} settings?".format(b["name"])
        if b["kind"] == "chromium":
            msg = ("Settings go back to their defaults. Extensions and their stored data (for example a wallet or password-manager "
                   "extension's local vault) move to the Trash with them; Undo brings everything back. Bookmarks, history, passwords and cookies stay.")
        else:
            msg = "Settings go back to their defaults. Extensions, bookmarks, history, passwords and cookies stay."
    if not confirm(title, msg, "Reset"):
        return None
    if not quit_browser_if_needed(b, "Resetting"):
        return "{} is still open, so nothing was reset".format(b["name"])
    try:
        res = browsers.reset(b, prof_ids, full)
    except Exception as e:  # noqa: BLE001
        return "Couldn't reset {}: {}".format(b["name"], e)
    alfred_search(KEYWORDS["browsers"] + " ")
    return "{} {} · undo with {}".format(b["name"], "fully reset" if full else "settings reset", KEYWORDS["hub"])


def browser_cache_all():
    import browsers
    done, skipped, freed = [], [], 0
    batch_label = "Clear browser caches"
    batch = engine.new_batch_id()
    for b in browsers.discover():
        if browsers.is_running(b) or (b["kind"] == "safari" and not b.get("access")):
            skipped.append(b["name"])
            continue
        try:
            res = browsers.clean(b, [], ["cache"], "all", label=batch_label, batch_id=batch)
            freed += res["freed"]
            done.append(b["name"])
        except Exception:  # noqa: BLE001
            skipped.append(b["name"])
    record_freed(freed)
    msg = "Cleared the cache of {}".format(", ".join(done)) if done else "No closed browsers to clear"
    if freed:
        msg += " · {}".format(format_bytes(freed))
    if skipped:
        msg += " · skipped {} (open)".format(", ".join(skipped))
    return msg


# ---------------------------------------------------------------------------
# butouchid — Touch ID for Sudo
# ---------------------------------------------------------------------------


def cmd_touchid(query):
    st = engine.touchid_status()
    if not st["supported"]:
        emit([item("Touch ID for Sudo Needs macOS 14 or Later", "This Mac has no /etc/pam.d/sudo_local", icon("warning"), valid=False)])
        return
    enabled = st["enabled"]
    status_item = item(
        "Touch ID for Sudo  —  {}".format("On" if enabled else "Off"),
        "Touch ID works for sudo in Terminal" if enabled else "sudo asks for your password",
        icon("touchid-" + ("green" if enabled else "red")), valid=False,
    )
    if st["legacy"]:
        toggle = item(
            "Turned On in /etc/pam.d/sudo",
            "Set up by hand, so macOS updates may reset it. Remove that line and turn it on here to make it permanent.",
            icon("info"), act("reveal", "/etc/pam.d/sudo"),
        )
    else:
        toggle = item(
            "Turn {} Touch ID for Sudo".format("Off" if enabled else "On"),
            "↩ 🔒 Asks for your password · survives macOS updates",
            icon("touchid-" + ("red" if enabled else "green")),
            act("touchid", "disable" if enabled else "enable"),
        )
    emit([status_item, toggle])


# ---------------------------------------------------------------------------
# bumenu — Burrow Companion (optional menu bar app and windows)
# ---------------------------------------------------------------------------

COMPANION_NAME = "Burrow Companion.app"
COMPANION_URL = "https://github.com/x-o-r-r-o/burrow/releases/latest"
# Left behind by Burrow 1.1 and earlier, which ran its helpers from inside the workflow
LEGACY_AGENT = os.path.join(HOME, "Library", "LaunchAgents", "io.github.burrow-alfred.menubar.plist")


def companion_path():
    for folder in ("/Applications", os.path.join(HOME, "Applications")):
        path = os.path.join(folder, COMPANION_NAME)
        if os.path.isdir(path):
            return path
    return None


def open_companion(section):
    path = companion_path()
    if path:
        subprocess.run(["/usr/bin/open", "-a", path, "burrow-companion://" + section])


def retire_legacy_helpers():
    """Stop and remove the menu bar helper older versions started from the workflow folder."""
    try:
        os.remove(LEGACY_AGENT)
    except OSError:
        pass
    subprocess.run(["/bin/launchctl", "bootout", "gui/{}/io.github.burrow-alfred.menubar".format(os.getuid())],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for line in engine.sh(["/bin/ps", "-Axo", "pid=,comm="]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].endswith(("/bin/BurrowMenu", "/bin/BurrowWindow")):
            try:
                os.kill(int(parts[0]), signal.SIGTERM)
            except OSError:
                pass


def cmd_menubar(query):
    if not companion_path():
        emit([
            item("Get Burrow Companion (Optional)",
                 "↩ Download page · menu bar health score, Updates and Browsers windows",
                 icon("menubar"), act("open", COMPANION_URL), mods={"cmd": mod("⌘ Copy the download link", act("copy", COMPANION_URL))}),
            item("Everything Else Works Without It", "Status, updates and browser cleaning are all available in Alfred", icon("check"), valid=False),
        ])
        return
    emit([
        item("Open Burrow Companion", "↩ Menu bar health score, refresh interval and Start at Login", icon("menubar"), act("companion", "menubar")),
        item("Open the Updates Window", "Release notes, update all, roll back, with progress for each app", icon("menubar"), act("companion", "updates")),
        item("Open the Browsers Window", "Choose what to clean with switches, for every browser", icon("menubar"), act("companion", "browsers")),
    ])


# ---------------------------------------------------------------------------
# Run Script dispatcher
# ---------------------------------------------------------------------------


def osascript(lines, *args):
    cmd = ["/usr/bin/osascript"]
    for line in lines:
        cmd += ["-e", line]
    return subprocess.run(cmd + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def alfred_search(text):
    osascript(["on run argv", 'tell application id "com.runningwithcrayons.Alfred" to search (item 1 of argv)', "end run"], text)


def notify(message):
    """Post an interim notification through the workflow's `notify` External Trigger."""
    osascript(
        ["on run argv", 'tell application id "com.runningwithcrayons.Alfred" to run trigger "notify" in workflow (item 1 of argv) with argument (item 2 of argv)', "end run"],
        BUNDLE_ID, message,
    )


def confirm(title, message, button):
    if len(message) > 1500:
        message = message[:1500] + "…"
    res = osascript(
        [
            "on run argv",
            "activate",
            "try",
            'display alert (item 1 of argv) message (item 2 of argv) as critical buttons {"Cancel", item 3 of argv} default button 2 cancel button 1 giving up after 300',
            "return (button returned of result) is (item 3 of argv)",
            "on error",
            "return false",
            "end try",
            "end run",
        ],
        title, message, button,
    )
    return res.stdout.decode().strip() == "true"


def trashed_message(done, failed):
    if failed:
        return "{} · couldn't move {}: {}".format(done, plural(len(failed), "item"), ", ".join(os.path.basename(f) for f in failed[:5]))
    return done


def cmd_run(arg):
    action = os.environ.get("action", "")
    target = os.environ.get("target", "") or arg
    try:
        payload = json.loads(os.environ.get("payload") or "{}")
    except ValueError:
        payload = {}
    try:
        message = dispatch(action, target, payload)
    except Exception as e:  # noqa: BLE001
        message = "Error: {}".format(e)
    if message:
        sys.stdout.write(message)


def dispatch(action, target, p):
    if action in ("", "noop"):
        return None
    if action == "alfred_search":
        alfred_search(target)
    elif action == "go":
        # target is a command name ("status") or "command query"
        command, _, query = target.partition(" ")
        if command in KEYWORDS:
            alfred_search(KEYWORDS[command] + " " + (os.environ.get("go_query") or query))
    elif action == "setup":
        return "Burrow is ready"
    elif action == "open":
        subprocess.run(["/usr/bin/open", target])
    elif action == "open_app":
        subprocess.run(["/usr/bin/open", "-a", target])
    elif action == "reveal":
        subprocess.run(["/usr/bin/open", "-R", target])
    elif action == "copy":
        subprocess.run(["/usr/bin/pbcopy"], input=target.encode())
        return "Copied: " + target
    elif action == "rescan":
        job_for(p.get("kind", ""), p.get("key", "")).clear()
        alfred_search(target)
    elif action in ("trash", "trash_many"):
        paths = p.get("paths") or [target]
        if p.get("confirm") and not confirm(p["confirm"], p.get("message", ""), p.get("button", "Remove")):
            return None
        failed = engine.trash_paths(paths, label=p.get("label") or p.get("done") or "Move to Trash")
        if not failed:
            record_freed(p.get("bytes", 0))
        for kind in p.get("invalidate", []):
            job_for(kind).clear()
        for key in p.get("invalidate_analyze", []):
            job_for("analyze", key).clear()
        for name in p.get("invalidate_memo", []):
            try:
                os.remove(engine.state_path("memo-" + name + ".json"))
            except OSError:
                pass
        if p.get("reopen"):
            alfred_search(p["reopen"])
        return trashed_message(p.get("done") or "Moved to the Trash", failed)
    elif action in ("clean_all", "clean_items"):
        return clean(p)
    elif action == "empty_trash":
        if not confirm("Empty Trash?", "Everything in the Trash will be permanently deleted. This can't be undone.", "Empty Trash"):
            return None
        r = osascript(['tell application "Finder" to empty trash'])
        return "Trash emptied" if r.returncode == 0 else "Couldn't empty the Trash: " + r.stderr.decode().strip()
    elif action == "optimize":
        return optimize(p)
    elif action == "undo":
        batch = engine.last_trash_batch()
        if not batch:
            return "Nothing to undo"
        open_apps = engine.batch_running_apps(batch)
        if open_apps:
            names = ", ".join(os.path.basename(a)[:-4] for a in open_apps)
            if not confirm("Quit {}?".format(names), "Undoing “{}” puts the previous version back, so {} needs to be closed.".format(batch["label"], names), "Quit and Undo"):
                return None
            for a in open_apps:
                if not engine.quit_app(a, engine.read_plist_key(engine.app_info_plist(a), "CFBundleIdentifier")):
                    return "{} didn't quit, so nothing was undone".format(os.path.basename(a)[:-4])
        restored, skipped = engine.undo_trash_batch(batch)
        for kind in ("clean", "purge"):
            job_for(kind).clear()
        msg = "Put back {}".format(plural(len(restored), "item"))
        if skipped:
            msg += " · {} couldn't be restored (already emptied from the Trash, or something new is in its place)".format(len(skipped))
        return msg
    elif action == "uninstall_toggle":
        app = p.get("app", "")
        engine.update_state(excluded_file(app), lambda d: {"paths": sorted(set(d.get("paths", [])) ^ {target})})
        alfred_search(KEYWORDS["uninstall"] + " =" + app)
    elif action == "clean_ignore":
        engine.update_state(engine.CLEAN_IGNORE, lambda d: {"paths": sorted(set(d.get("paths", [])) | {target})})
        job_for("clean").clear()
        alfred_search(KEYWORDS["clean"] + " ")
        return "{} won't be cleaned any more".format(p.get("description") or os.path.basename(target))
    elif action == "clean_unignore":
        engine.save_state(engine.CLEAN_IGNORE, {"paths": []})
        job_for("clean").clear()
        alfred_search(KEYWORDS["clean"] + " ")
    elif action in ("companion", "window"):
        open_companion(target or "updates")
    elif action == "browser_toggle":
        cur = browser_choice(target)["categories"]
        cat = p.get("category")
        set_browser_choice(target, categories=[c for c in cur if c != cat] if cat in cur else cur + [cat])
        alfred_search(KEYWORDS["browsers"] + " =" + target)
    elif action == "browser_choice":
        set_browser_choice(target, **{k: v for k, v in p.items() if k in ("range", "profile")})
        alfred_search(KEYWORDS["browsers"] + " =" + target)
    elif action == "browser_clean":
        return browser_clean_action(target)
    elif action == "browser_reset":
        return browser_reset_action(target, bool(p.get("full")))
    elif action == "browser_cache_all":
        return browser_cache_all()
    elif action == "update_install":
        return install_updates(p.get("paths") or [])
    elif action == "update_ignore":
        import updates
        updates.set_ignore(target, p.get("rule"))
        alfred_search(KEYWORDS["updates"] + " ")
        return "Won't show {} again".format(p.get("name")) if p.get("rule") == "*" else "Skipping {} {}".format(p.get("name"), p.get("rule"))
    elif action == "update_unignore":
        engine.save_state("updates-ignore.json", {})
        alfred_search(KEYWORDS["updates"] + " ")
    elif action == "dupes_trash_all":
        groups = p.get("groups") or []
        count = sum(len(g["trash"]) for g in groups)
        if not groups or not confirm(
                "Keep the newest copy of {}?".format(plural(len(groups), "file")),
                "{} will move to the Trash. Each is checked again first, and anything that changed since the scan is kept.".format(plural(count, "older copy")),
                "Move to Trash"):
            return None
        batch = engine.new_batch_id()
        moved = kept = 0
        failed = []
        for g in groups:
            keep = g["keep"] if os.path.isfile(g["keep"]) and not os.path.islink(g["keep"]) else None
            if not keep:
                kept += len(g["trash"])
                continue
            same = engine.verify_duplicates(keep, g["trash"])
            kept += len(g["trash"]) - len(same)
            f = engine.trash_paths(same, label="Duplicates (keep newest)", batch_id=batch)
            failed += f
            moved += len(same) - len(f)
            record_freed(g["size"] * (len(same) - len(f)))
        if p.get("reopen"):
            alfred_search(p["reopen"])
        msg = "Moved {} to the Trash".format(plural(moved, "duplicate"))
        if kept:
            msg += " · kept {} that changed since the scan".format(kept)
        return trashed_message(msg, failed)
    elif action == "dupes_trash":
        return dupes_trash(p)
    elif action == "startup_remove":
        entries = p.get("entries") or []
        if not entries:
            return None
        names = "\n".join("• {} ({})".format(e["owner"] or e["label"], tilde(e["path"])) for e in entries[:20])
        if not confirm("Remove {}?".format(plural(len(entries), "startup item")),
                       "These will stop running and their files will move to the Trash:\n\n{}".format(names),
                       "Remove"):
            return None
        failed = engine.remove_startup_items(entries, batch_id=engine.new_batch_id())
        alfred_search(KEYWORDS["startup"] + " ")
        return trashed_message("Removed {}".format(plural(len(entries) - len(failed), "startup item")), failed)
    elif action == "proc_quit":
        msg = proc_quit(p, bool(p.get("force")))
        if msg:
            alfred_search(KEYWORDS["processes"] + " ")
        return msg
    elif action == "proc_restart":
        return proc_restart(p)
    elif action == "proc_view":
        view = process_view()
        view.update({k: p[k] for k in ("sort", "group") if k in p})
        engine.save_state(PROCESS_VIEW, view)
        alfred_search(KEYWORDS["processes"] + " ")
    elif action == "uninstall":
        return uninstall(target, p.get("name") or os.path.basename(target)[:-4], p.get("size") or 0,
                         excluded=set(p.get("excluded") or []), reviewed=p.get("reviewed", False), reset=p.get("reset", False))
    elif action == "touchid":
        ok, out = engine.touchid_set(target == "enable")
        alfred_search(KEYWORDS["touchid"] + " ")
        if ok is None:
            return None
        if not ok:
            return "Couldn't change Touch ID for sudo: " + out
        return "Touch ID for sudo turned {}".format("on" if target == "enable" else "off")
    else:
        return "Unknown action: " + action
    return None


def dupes_trash(p):
    """Trash duplicate copies, but only after re-checking, right now, that a kept
    copy still exists and each file to trash is still byte-identical to it."""
    keep = next((k for k in p.get("keep", []) if os.path.isfile(k) and not os.path.islink(k)), None)
    if not keep:
        return "Nothing was trashed: the copy to keep is gone. Rescan duplicates"
    if p.get("confirm") and not confirm(p["confirm"], p.get("message", ""), p.get("button", "Move to Trash")):
        return None
    same = engine.verify_duplicates(keep, p.get("trash", []))
    changed = [x for x in p.get("trash", []) if x not in same and os.path.lexists(x)]
    failed = engine.trash_paths(same, label=p.get("label") or "Duplicates")
    if same and not failed:
        record_freed(p.get("size", 0) * len(same))
    for key in [k for k in (p.get("reopen"),) if k]:
        alfred_search(key)
    msg = "Moved {} to the Trash".format(plural(len(same) - len(failed), "duplicate"))
    if changed:
        msg += " · kept {} that changed since the scan".format(plural(len(changed), "file"))
    return trashed_message(msg, failed)


def clean(p):
    """Move the previewed clean items (all, or the named ones) to the Trash."""
    job = job_for("clean")
    found, _ = clean_results(job)
    if p.get("keys"):
        found = [i for i in found if i["paths"][0] in p["keys"]]
    else:
        found = [i for i in found if not i.get("optional")]
    if not found:
        return "Nothing to clean. Run the scan again."
    notify("Checking nothing is in use…") if len(found) > 3 else None
    activity = engine.Activity()
    busy = [i for i in found if engine.item_busy(i, activity)]
    found = [i for i in found if i not in busy]
    if not found:
        return "Nothing cleaned: {} in use right now. Quit it and try again".format(
            busy[0]["description"] if len(busy) == 1 else plural(len(busy), "item") + " are")
    total = sum(i["size"] for i in found)
    what = found[0]["description"] if len(found) == 1 else plural(len(found), "item")
    if not confirm(
        "Clean {}?".format(what),
        "{} of caches, logs and temporary files will be moved to the Trash. Apps recreate these files when they need them.{}\n\nEmpty the Trash afterwards to free the space.".format(
            format_bytes(total), "\n\nSystem items need your password, and Finder will ask for it." if any(i.get("admin") for i in found) else ""),
        "Move to Trash",
    ):
        return None
    if len(found) > 3:
        notify("Cleaning {}…".format(format_bytes(total)))
    needs_admin = any(i.get("admin") for i in found)
    failed = engine.trash_paths([path for i in found for path in i["paths"]], finder_fallback=needs_admin, label="Clean " + what)
    if not failed:
        record_freed(total)
    job.clear()
    msg = "{} moved to the Trash. Empty it to free the space, or undo with {}".format(format_bytes(total), KEYWORDS["hub"])
    if busy:
        msg += " · skipped {} now in use".format(plural(len(busy), "item"))
    return trashed_message(msg, failed)


def optimize(p):
    ids = p.get("ids") or []
    tasks = [t for t in engine.optimizations() if t["id"] in ids]
    if not tasks:
        return None
    if p.get("confirm"):
        listing = "\n".join("• " + t["title"] for t in tasks)
        if not confirm("Optimize System", "Burrow will run:\n\n{}\n\nSome changes take effect after a restart.".format(listing), "Optimize"):
            return None
    if len(tasks) > 1:
        notify("Optimizing…")
    res = engine.run_optimizations(ids)
    if res["cancelled"] and not res["ok"]:
        return None
    parts = []
    if res["ok"]:
        parts.append("Done: " + ", ".join(res["ok"]))
    if res["failed"]:
        parts.append("Failed: " + ", ".join(res["failed"]))
    if res["cancelled"]:
        parts.append("Skipped the tasks that need your password")
    return " · ".join(parts)


# Leftovers that hold the app's data and settings. "Reset" removes only these,
# so the app starts fresh; a full uninstall removes everything.
DATA_LOCATIONS = {
    "Application Support", "Caches", "Containers", "Group Containers", "Preferences", "Preferences/ByHost",
    "Saved Application State", "HTTPStorages", "WebKit", "Cookies", "Logs", "Application Scripts",
    "System cache folder", "System temp folder", "Crash reports", "Application Support/CrashReporter",
    "Logs/DiagnosticReports",
}


def is_data(r):
    return r["location"] in DATA_LOCATIONS or r["location"].startswith(("Application Support/", "Caches/", "Logs/", "Library/Application Support"))


def uninstall(app_path, name, app_size, excluded=frozenset(), reviewed=False, reset=False):
    """Uninstall (or reset) an app: quit it, stop its background items, move the
    app and its leftovers to the Trash (system-owned ones with one password
    prompt), unload its system daemons and take it out of the Dock. One Undo step."""
    if not reviewed:
        notify("Looking for {}'s leftover files…".format(name))
    ids = engine.app_identifiers(app_path)
    residuals = [r for r in engine.find_residual_files(name, ids, app_path) if r["path"] not in excluded]
    if reset:
        residuals = [r for r in residuals if is_data(r)]
    res_total = sum(r["size"] for r in residuals)
    root_items = [r for r in residuals if engine.needs_root(r["path"])]
    app_needs_root = not reset and engine.needs_root(app_path)
    is_running = engine.app_is_running(app_path)

    if residuals:
        listing = "\n".join("  • {} ({}){}".format(os.path.basename(r["path"]), r["location"], " 🔒" if r in root_items else "") for r in residuals[:25])
        if len(residuals) > 25:
            listing += "\n  … and {} more".format(len(residuals) - 25)
        res_msg = "\n\n{} ({}):\n{}".format(plural(len(residuals), "leftover item"), format_bytes(res_total), listing)
    else:
        res_msg = "\n\nNo leftover files found."
    if reset:
        title, button = "Reset {}?".format(name), "Reset"
        msg = "{}'s settings, caches and data will be moved to the Trash. The app stays installed and starts fresh.".format(name)
    else:
        title, button = "Uninstall {}?".format(name), "Uninstall"
        total = app_size + res_total
        msg = "{}.app and everything it left behind will be moved to the Trash.{}".format(name, " Total: {}.".format(format_bytes(total)) if total else "")
    if is_running:
        msg = "{} is running and will be quit first.\n\n".format(name) + msg
    if root_items or app_needs_root:
        msg += "\n\nItems marked 🔒 belong to the system, so macOS will ask for your password once."
    if not confirm(title, msg + res_msg, button):
        return None

    if is_running and not engine.quit_app(app_path, ids.get("bundle_id")):
        if not confirm("{} didn't quit".format(name), "It may be showing a dialog or waiting to save. Force quit it? Unsaved changes will be lost.", "Force Quit"):
            return "{} is still running, so nothing was removed".format(name)
        if not engine.force_quit(app_path):
            return "{} couldn't be quit, so nothing was removed".format(name)

    engine.stop_background_items(app_path, residuals)
    label = ("Reset " if reset else "Uninstall ") + name
    batch = engine.new_batch_id()

    if not reset:
        # The app goes first: if it can't be moved, its settings stay where they are.
        if app_needs_root:
            failed_app = engine.admin_trash([app_path], label, batch, prompt="Burrow needs your password to uninstall {}.".format(name))
        else:
            failed_app = engine.trash_paths([app_path], finder_fallback=False, label=label, batch_id=batch)
        if failed_app:
            return "Couldn't move {}.app to the Trash, so its files were left alone".format(name)

    user_paths = [r["path"] for r in residuals if r not in root_items and os.path.lexists(r["path"])]
    failed = engine.trash_paths(user_paths, finder_fallback=False, label=label, batch_id=batch)
    if root_items:
        import shlex
        daemons = [r["path"] for r in root_items if "/LaunchDaemons/" in r["path"] and r["path"].endswith(".plist")]
        before = ["launchctl bootout system {} 2>/dev/null".format(shlex.quote(d)) for d in daemons]
        failed += engine.admin_trash([r["path"] for r in root_items if os.path.lexists(r["path"])], label, batch,
                                     prompt="Burrow needs your password to remove {}'s system files.".format(name), before=before)
    in_dock = False if reset else engine.remove_from_dock(app_path)
    if not failed:
        record_freed(res_total + (0 if reset else app_size))

    for f in ("app-sizes.json",):
        try:
            os.remove(os.path.join(CACHE_DIR, f))
        except OSError:
            pass
    for f in (excluded_file(app_path), "memo-residuals-" + hashlib.sha1(app_path.encode()).hexdigest()[:12] + ".json", "memo-orphans.json"):
        try:
            os.remove(engine.state_path(f))
        except OSError:
            pass
    alfred_search(KEYWORDS["uninstall"] + " " + ("=" + app_path if reset else ""))
    removed = len(residuals) - len(failed)
    if reset:
        msg = "{} reset · {} removed".format(name, plural(removed, "item"))
    else:
        msg = "{} uninstalled".format(name) + (" with {}".format(plural(removed, "leftover")) if removed else "")
        if in_dock:
            msg += " · removed from the Dock"
    if failed:
        msg += " · {} couldn't be moved".format(len(failed))
    return msg + " · undo with " + KEYWORDS["hub"]


COMMANDS = {
    "hub": cmd_hub,
    "status": cmd_status,
    "clean": cmd_clean,
    "optimize": cmd_optimize,
    "uninstall": cmd_uninstall,
    "purge": cmd_purge,
    "analyze": cmd_analyze,
    "installer": cmd_installer,
    "touchid": cmd_touchid,
    "menubar": cmd_menubar,
    "large": cmd_large,
    "startup": cmd_startup,
    "dupes": cmd_dupes,
    "updates": cmd_updates,
    "browsers": cmd_browsers,
    "processes": cmd_processes,
    "run": cmd_run,
}


def log_error(command):
    import traceback
    try:
        path = os.path.join(CACHE_DIR, "burrow.log")
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + ".1")
        with open(path, "a") as f:
            f.write("{} {}\n{}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), command, traceback.format_exc()))
        return path
    except OSError:
        return None


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.stderr.write("usage: burrow.py <{}> [query]\n".format("|".join(COMMANDS)))
        sys.exit(2)
    os.makedirs(CACHE_DIR, exist_ok=True)
    command = sys.argv[1]
    try:
        engine.housekeeping()  # a single file check; the tidy-up itself runs once a day
        marker = os.path.join(CACHE_DIR, ".legacy-helpers-retired")
        if os.environ.get("alfred_version") and not os.path.exists(marker):
            retire_legacy_helpers()
            open(marker, "w").close()
        # Only when run by Alfred (it sets alfred_version), never from tests or a terminal
        if command in ("updates", "hub", "run") and os.environ.get("alfred_version"):
            import updates
            updates.sync_agent(os.environ.get("auto_updates", "off"), os.path.join(WF_DIR, "updates.py"))
    except Exception:  # noqa: BLE001
        pass
    try:
        COMMANDS[command](sys.argv[2] if len(sys.argv) > 2 else "")
    except Exception as e:  # noqa: BLE001 — show the problem instead of an empty list
        log = log_error(command)
        if command == "run":
            sys.stdout.write("Burrow hit an error: {}".format(e))
            return
        emit([item(
            "Something Went Wrong",
            "{}: {} · ↩ Open the log".format(type(e).__name__, e),
            icon("error"), act("open", log or CACHE_DIR),
        )])


if __name__ == "__main__":
    main()
