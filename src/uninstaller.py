"""JSON commands for Burrow Companion's Uninstaller window. Uses the same engine as
`buuninstall` in Alfred, so what's found, what's kept and Undo are shared.

    uninstaller.py cli list                 installed apps with size and last use
    uninstaller.py cli review APP           what uninstalling APP removes
    uninstaller.py cli toggle APP PATH      keep a leftover (or include it again)
    uninstaller.py cli uninstall JSON       {"path", "reset", "force"}: uninstall or reset
    uninstaller.py cli undo                 put back the last thing Burrow moved to the Trash

Each command prints JSON lines.
"""

import json
import os
import sys
import time

import burrow
import engine


def say(obj):
    try:
        print(json.dumps(obj), flush=True)
    except BrokenPipeError:
        sys.stdout = open(os.devnull, "w")  # the window went away: keep working


def _sizes():
    """Cached app sizes and last-used dates; starts a refresh in the background when stale."""
    sizes, last_used, stale = {}, {}, True
    try:
        with open(burrow.app_sizes_file()) as f:
            data = json.load(f)
        if "sizes" in data:
            sizes, last_used = data["sizes"], data.get("last_used", {})
            stale = time.time() - os.path.getmtime(burrow.app_sizes_file()) > burrow.APP_SIZES_TTL
    except (OSError, ValueError):
        pass
    if stale:
        burrow.job_for("app-sizes").ensure(burrow.engine_argv("app-sizes", burrow.app_sizes_file()), ttl=60, timeout=600)
    return sizes, last_used, stale


def _last_undo():
    batch = engine.last_trash_batch()
    return batch["label"] if batch else None


def list_apps():
    sizes, last_used, stale = _sizes()
    running = engine.running_executables()
    apps = []
    for a in engine.list_apps():
        if a["name"].startswith("Alfred "):  # the app running Burrow
            continue
        apps.append({
            "name": a["name"], "path": a["path"], "size": sizes.get(a["path"], 0),
            "last_used": last_used.get(a["path"]), "known_use": a["path"] in last_used,
            "mtime": a["mtime"] if a["mtime"] > 946684800 else None,
            "running": engine.app_is_running(a["path"], running),
        })
    return {"apps": apps, "sizing": stale, "unused_days": burrow.UNUSED_DAYS, "undo": _last_undo()}


def review(app_path):
    if not app_path.endswith(".app") or not os.path.isdir(app_path):
        return {"error": "This app isn't installed any more"}
    name = os.path.basename(app_path)[:-4]
    scan = burrow.residuals_for(app_path, name)
    excluded = set(engine.load_state(burrow.excluded_file(app_path)).get("paths", []))
    sizes, _, _ = _sizes()
    info = engine.app_identifiers(app_path)
    version = engine.read_plist_key(engine.app_info_plist(app_path), "CFBundleShortVersionString") or ""
    items = [{
        "path": r["path"], "name": os.path.basename(r["path"]), "location": r["location"], "size": r["size"],
        "locked": engine.needs_root(r["path"]), "data": burrow.is_data(r), "kept": r["path"] in excluded,
    } for r in scan["residuals"] if os.path.lexists(r["path"])]
    return {
        "path": app_path, "name": name, "version": str(version), "bundle_id": info.get("bundle_id"),
        "size": sizes.get(app_path, 0) or engine.dir_size(app_path),
        "locked": engine.needs_root(app_path), "running": engine.app_is_running(app_path),
        "items": items,
        "extensions": [e["name"] for e in scan["extensions"]],
        "uninstallers": scan["uninstallers"],
    }


def toggle(app_path, path):
    engine.update_state(burrow.excluded_file(app_path), lambda d: {"paths": sorted(set(d.get("paths", [])) ^ {path})})
    return {"ok": True}


def uninstall(req):
    """The window has already asked; Burrow's own dialogs are answered here. If the app
    doesn't quit when asked, it's force quit only when the window said so."""
    app_path = req.get("path", "")
    if not os.path.isdir(app_path):
        return {"error": "This app isn't installed any more"}
    name = os.path.basename(app_path)[:-4]
    force = bool(req.get("force"))
    burrow.confirm = lambda title, message, button: force if title.endswith("didn't quit") else True
    burrow.notify = lambda *a, **k: None
    burrow.alfred_search = lambda *a, **k: None
    excluded = set(engine.load_state(burrow.excluded_file(app_path)).get("paths", []))
    sizes, _, _ = _sizes()
    size = sizes.get(app_path, 0) or engine.dir_size(app_path)
    msg = burrow.uninstall(app_path, name, size, excluded=excluded, reviewed=True, reset=bool(req.get("reset")))
    if not msg:
        return {"error": "Cancelled"}
    msg = msg.split(" · undo with ")[0]
    ok = not (msg.endswith("nothing was removed") or msg.startswith("Couldn't"))
    return {"done": msg} if ok else {"error": msg, "running": "still running" in msg}


def undo():
    burrow.confirm = lambda *a, **k: True  # the window asked before undoing
    label = _last_undo()
    if not label:
        return {"error": "Nothing to undo"}
    msg = burrow.dispatch("undo", "", {})
    return {"done": "{}: {}".format(label, msg)}


def cli(argv):
    cmd = argv[0] if argv else ""
    try:
        if cmd == "list":
            say(list_apps())
        elif cmd == "review" and len(argv) > 1:
            say(review(argv[1]))
        elif cmd == "toggle" and len(argv) > 2:
            say(toggle(argv[1], argv[2]))
        elif cmd == "uninstall" and len(argv) > 1:
            say(uninstall(json.loads(argv[1])))
        elif cmd == "undo":
            say(undo())
        else:
            say({"error": "unknown command"})
    except Exception as e:  # noqa: BLE001
        say({"error": str(e)})


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        cli(sys.argv[2:])
    else:
        print(__doc__)
