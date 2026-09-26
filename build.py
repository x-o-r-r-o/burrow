#!/usr/bin/python3
"""Assemble the Alfred workflow: writes build/ and dist/Burrow.alfredworkflow.

    python3 build.py            # build
    python3 build.py --install  # build, then open the package so Alfred imports it
"""

import os
import plistlib
import shutil
import subprocess
import sys
import uuid
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as _f:
    VERSION = _f.read().strip()
REPO = "x-o-r-r-o/burrow"
BUNDLE_ID = "io.github.burrow-alfred"

CMD, ALT, CTRL = 1048576, 524288, 262144

# (command, keyword, title, subtext, icon, running subtext)
SCRIPT_FILTERS = [
    ("hub", "bu", "Burrow", "All Burrow commands", "status", "Loading…"),
    ("status", "bustatus", "System Status", "Health, CPU, memory, disk, battery and network", "status", "Reading system status…"),
    ("updates", "buupdates", "App Updates", "Check your apps for new versions and install them safely", "update", "Checking…"),
    ("clean", "buclean", "Clean System", "Preview and remove caches, logs and temporary files", "clean", "Scanning…"),
    ("optimize", "buoptimize", "Optimize System", "Flush DNS, rebuild databases, refresh services", "optimize", "Checking…"),
    ("uninstall", "buuninstall", "Uninstall App", "Remove apps and their leftover files", "uninstall", "Listing apps…"),
    ("analyze", "buanalyze", "Analyze Disk", "Browse folders sorted by size", "analyze", "Analyzing…"),
    ("purge", "bupurge", "Purge Dev Artifacts", "Remove old node_modules, .next, dist, target, venv…", "purge", "Scanning projects…"),
    ("installer", "buinstallers", "Clean Installers", "Remove .dmg, .pkg and .iso files", "installer", "Scanning…"),
    ("touchid", "butouchid", "Touch ID for Sudo", "Check and toggle Touch ID for sudo", "touchid-green", "Checking…"),
    ("large", "bularge", "Large Files", "Find the biggest files in your home folder", "large", "Searching…"),
    ("dupes", "budupes", "Duplicate Files", "Find identical copies of files", "dupes", "Searching…"),
    ("startup", "bustartup", "Startup Items", "Launch agents and daemons", "startup", "Loading…"),
    ("menubar", "bumenu", "Menu Bar Health", "Show the health score in the menu bar", "menubar", "Checking…"),
]


_UID_NAMESPACE = uuid.UUID("7d3c9f1e-5b1a-4a4e-9a57-6b2f0c2b8e11")
_uid_counter = {}


def uid(name):
    """A stable id per object, so updating the workflow keeps anything the user
    attached to an object (hotkeys, keyword edits) instead of replacing it."""
    n = _uid_counter.get(name, 0)
    _uid_counter[name] = n + 1
    return str(uuid.uuid5(_UID_NAMESPACE, "{}#{}".format(name, n))).upper()


def conn(dest, modifiers=0, subtext=""):
    return {"destinationuid": dest, "modifiers": modifiers, "modifiersubtext": subtext, "vitoclose": False}


def build_plist(icon_for):
    objects, connections, uidata = [], {}, {}

    run_uid = uid("run-script")
    objects.append(
        {
            "uid": run_uid,
            "type": "alfred.workflow.action.script",
            "version": 2,
            "config": {
                "concurrently": True,
                "escaping": 102,
                "script": '/bin/bash ./run.sh run "$1"',
                "scriptargtype": 1,
                "scriptfile": "",
                "type": 0,
            },
        }
    )
    uidata[run_uid] = {"xpos": 500, "ypos": 500, "note": "Performs the selected action (variables: action, target, payload)"}

    notif_uid = uid("notification")
    objects.append(
        {
            "uid": notif_uid,
            "type": "alfred.workflow.output.notification",
            "version": 1,
            "config": {
                "lastpathcomponent": False,
                "onlyshowifquerypopulated": True,
                "removeextension": False,
                "text": "{query}",
                "title": "Burrow",
            },
        }
    )
    uidata[notif_uid] = {"xpos": 750, "ypos": 500}
    connections[run_uid] = [conn(notif_uid)]

    for i, (command, keyword, title, subtext, icon, running) in enumerate(SCRIPT_FILTERS):
        sf = uid("scriptfilter-" + command)
        objects.append(
            {
                "uid": sf,
                "type": "alfred.workflow.input.scriptfilter",
                "version": 3,
                "config": {
                    "alfredfiltersresults": False,
                    "alfredfiltersresultsmatchmode": 0,
                    "argumenttreatemptyqueryasnil": False,
                    "argumenttrimmode": 0,
                    "argumenttype": 1,
                    "escaping": 102,
                    "keyword": keyword,
                    "queuedelaycustom": 3,
                    "queuedelayimmediatelyinitially": True,
                    "queuedelaymode": 0,
                    "queuemode": 2,
                    "runningsubtext": running,
                    "script": '/bin/bash ./run.sh {} "$1"'.format(command),
                    "scriptargtype": 1,
                    "scriptfile": "",
                    "subtext": subtext,
                    "title": title,
                    "type": 0,
                    "withspace": True,
                },
            }
        )
        icon_for[sf] = icon
        uidata[sf] = {"xpos": 50, "ypos": 20 + i * 120}
        connections[sf] = [conn(run_uid), conn(run_uid, CMD), conn(run_uid, ALT), conn(run_uid, CTRL)]

    # External trigger used by the script to post progress notifications.
    ext = uid("trigger-notify")
    objects.append(
        {
            "uid": ext,
            "type": "alfred.workflow.trigger.external",
            "version": 1,
            "config": {"availableviaurlhandler": False, "triggerid": "notify"},
        }
    )
    uidata[ext] = {"xpos": 500, "ypos": 680, "note": "Progress notifications from burrow.py"}
    connections[ext] = [conn(notif_uid)]

    for name, types, command, query, icon, note in (
        ("Uninstall with Burrow", ["com.apple.application-bundle"], "uninstall", "={query}", "uninstall", "File action on apps"),
        ("Analyze with Burrow", ["public.folder"], "analyze", "{query}/", "analyze", "File action on folders"),
        ("Find Duplicates with Burrow", ["public.folder"], "dupes", "@{query}/", "dupes", "File action on folders"),
    ):
        fa = uid("fileaction-" + command)
        icon_for[fa] = icon
        objects.append({
            "uid": fa,
            "type": "alfred.workflow.trigger.action",
            "version": 1,
            "config": {"acceptsfiles": True, "acceptsmulti": 0, "acceptstext": False, "acceptsurls": False, "filetypes": types, "name": name},
        })
        fa_vars = uid("fileaction-vars-" + command)
        objects.append({
            "uid": fa_vars,
            "type": "alfred.workflow.utility.argument",
            "version": 1,
            "config": {"argument": "{query}", "passthroughargument": False, "variables": {"action": "go", "target": command, "go_query": query, "payload": ""}},
        })
        n = len(uidata)
        uidata[fa] = {"xpos": 50, "ypos": 20 + (len(SCRIPT_FILTERS) + 1) * 120 + n * 5, "note": note}
        uidata[fa_vars] = {"xpos": 300, "ypos": 50 + (len(SCRIPT_FILTERS) + 1) * 120 + n * 5}
        connections[fa] = [conn(fa_vars)]
        connections[fa_vars] = [conn(run_uid)]

    open_uid = uid("trigger-open")
    objects.append({
        "uid": open_uid,
        "type": "alfred.workflow.trigger.external",
        "version": 1,
        "config": {"availableviaurlhandler": True, "triggerid": "open"},
    })
    uidata[open_uid] = {"xpos": 50, "ypos": 20 + len(SCRIPT_FILTERS) * 120, "note": "Menu bar app: open a Burrow command in Alfred"}
    open_vars = uid("open-vars")
    objects.append({
        "uid": open_vars,
        "type": "alfred.workflow.utility.argument",
        "version": 1,
        "config": {"argument": "{query}", "passthroughargument": False, "variables": {"action": "go", "target": "{query}", "go_query": "", "payload": ""}},
    })
    uidata[open_vars] = {"xpos": 300, "ypos": 50 + len(SCRIPT_FILTERS) * 120}
    connections[open_uid] = [conn(open_vars)]
    connections[open_vars] = [conn(run_uid)]

    with open(os.path.join(ROOT, "README.md")) as f:
        readme = f.read()

    return {
        "bundleid": BUNDLE_ID,
        "category": "Tools",
        "connections": connections,
        "createdby": "Burrow",
        "description": "Dig out the clutter: deep clean and optimize your Mac",
        "disabled": False,
        "name": "Burrow",
        "objects": objects,
        "readme": readme,
        "uidata": uidata,
        "userconfigurationconfig": [
            {
                "type": "popupbutton",
                "variable": "status_refresh",
                "label": "Status Refresh",
                "description": "How often System Status refreshes while open.",
                "config": {"default": "3", "pairs": [["1 Second", "1"], ["3 Seconds", "3"], ["5 Seconds", "5"]]},
            },
            {
                "type": "textfield",
                "variable": "analyze_path",
                "label": "Analyze Default Path",
                "description": "Folder Analyze Disk opens when no path is typed. Leave empty to pick from common locations.",
                "config": {"default": "", "placeholder": "~/", "required": False, "trim": True},
            },
            {
                "type": "popupbutton",
                "variable": "auto_updates",
                "label": "Automatic Update Checks",
                "description": "Once a day, check your apps for updates. “Install” also installs updates for apps that aren't open, after verifying each download.",
                "config": {"default": "notify", "pairs": [["Off", "off"], ["Notify me", "notify"], ["Install automatically", "install"]]},
            },
            {
                "type": "popupbutton",
                "variable": "menubar_interval",
                "label": "Menu Bar Refresh",
                "description": "How often the menu bar health score updates.",
                "config": {"default": "30", "pairs": [["10 Seconds", "10"], ["30 Seconds", "30"], ["1 Minute", "60"], ["5 Minutes", "300"]]},
            },
            {
                "type": "popupbutton",
                "variable": "large_min_mb",
                "label": "Large File Size",
                "description": "Large Files lists files bigger than this.",
                "config": {"default": "500", "pairs": [["100 MB", "100"], ["250 MB", "250"], ["500 MB", "500"], ["1 GB", "1024"], ["5 GB", "5120"]]},
            },
            {
                "type": "textfield",
                "variable": "project_dirs",
                "label": "Project Folders",
                "description": "Comma-separated folders Purge Dev Artifacts searches. Leave empty to check ~/Projects, ~/Developer, ~/Code, ~/Documents and other common places.",
                "config": {"default": "", "placeholder": "~/Projects, ~/Work", "required": False, "trim": True},
            },
            {
                "type": "popupbutton",
                "variable": "purge_min_days",
                "label": "Purge Minimum Age",
                "description": "Only list build artifacts in projects that haven't been touched for this long.",
                "config": {"default": "7", "pairs": [["Any age", "0"], ["1 Week", "7"], ["2 Weeks", "14"], ["1 Month", "30"], ["3 Months", "90"]]},
            },
        ],
        "variablesdontexport": [],
        "version": VERSION,
        "webaddress": "https://github.com/" + REPO,
    }


def build_swift(src, out):
    """Compile a Swift helper as a universal (Apple silicon + Intel) binary."""
    if not shutil.which("swiftc"):
        sys.exit("build.py: the Swift compiler (swiftc) isn't installed. Install Apple's Command Line Tools: xcode-select --install")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = os.path.join(BUILD, ".swift")
    os.makedirs(tmp, exist_ok=True)
    slices = []
    for arch in ("arm64", "x86_64"):
        dest = os.path.join(tmp, os.path.basename(out) + "-" + arch)
        res = subprocess.run(["swiftc", "-O", "-target", arch + "-apple-macos13", src, "-o", dest], stderr=subprocess.PIPE)
        if res.returncode != 0:
            errors = [l for l in res.stderr.decode("utf-8", "replace").splitlines() if "warning:" not in l]
            sys.exit("build.py: compiling {} for {} failed:\n{}".format(os.path.basename(src), arch, "\n".join(errors[-20:])))
        slices.append(dest)
    subprocess.run(["lipo", "-create"] + slices + ["-output", out], check=True)
    subprocess.run(["codesign", "-s", "-", "--force", out], check=True, stderr=subprocess.PIPE)
    shutil.rmtree(tmp)


def main():
    if not shutil.which("swiftc"):  # check before touching the previous build
        sys.exit("build.py: the Swift compiler (swiftc) isn't installed. Install Apple's Command Line Tools: xcode-select --install")
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
    os.makedirs(DIST, exist_ok=True)

    for name in ("burrow.py", "engine.py", "updates.py", "run.sh"):
        shutil.copy(os.path.join(SRC, name), BUILD)
    os.chmod(os.path.join(BUILD, "run.sh"), 0o755)
    build_swift(os.path.join(ROOT, "tools", "menubar", "BurrowMenu.swift"), os.path.join(BUILD, "bin", "BurrowMenu"))
    build_swift(os.path.join(ROOT, "tools", "trash", "BurrowTrash.swift"), os.path.join(BUILD, "bin", "BurrowTrash"))
    with open(os.path.join(BUILD, "bin", ".stamp"), "w") as f:
        f.write("{}-{}\n".format(VERSION, uuid.uuid4().hex[:12]))
    shutil.copytree(os.path.join(SRC, "icons"), os.path.join(BUILD, "icons"), ignore=shutil.ignore_patterns(".*", "__pycache__"))
    shutil.copy(os.path.join(SRC, "icon.png"), os.path.join(BUILD, "icon.png"))

    icon_for = {}
    info = build_plist(icon_for)
    for obj_uid, name in icon_for.items():
        shutil.copy(os.path.join(SRC, "icons", name + ".png"), os.path.join(BUILD, obj_uid + ".png"))
    with open(os.path.join(BUILD, "info.plist"), "wb") as f:
        plistlib.dump(info, f)

    out = os.path.join(DIST, "Burrow.alfredworkflow")
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _, files in os.walk(BUILD):
            for name in sorted(files):
                path = os.path.join(base, name)
                z.write(path, os.path.relpath(path, BUILD))
    print("built", out)

    if "--install" in sys.argv:
        subprocess.run(["/usr/bin/open", out])


if __name__ == "__main__":
    main()
