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
    ("hub", "burrow", "Burrow", "All Burrow commands", "status", "Loading…"),
    ("status", "bustatus", "System Status", "Health, CPU, memory, disk, battery and network", "status", "Reading system status…"),
    ("browsers", "bubrowsers", "Browsers", "Clear history, cache, cookies and more, or reset a browser", "browser", "Finding browsers…"),
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
    ("menubar", "bumenu", "Burrow Companion", "Optional app: health in the menu bar, Updates and Browsers windows", "menubar", "Checking…"),
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


def keyword_fields():
    """One editable keyword per command (Workflow's Configuration)."""
    fields = []
    for command, keyword, title, subtext, icon, running in SCRIPT_FILTERS:
        fields.append({
            "type": "textfield",
            "variable": "keyword_" + command,
            "label": "{} keyword".format(title if command != "hub" else "Main"),
            "description": subtext + "." if command != "hub" else "Lists every Burrow command.",
            "config": {"default": keyword, "placeholder": keyword, "required": True, "trim": True},
        })
    return fields


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
                    "keyword": "{var:keyword_%s}" % command,
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
        # Alfred shows the workflow's name and icon itself; start at the description
        readme = f.read().split("\n", 1)[1].strip() + "\n"

    return {
        "bundleid": BUNDLE_ID,
        "category": "Tools",
        "connections": connections,
        "createdby": "x-o-r-r-o",
        "description": "Dig out the clutter: deep clean and optimize your Mac",
        "disabled": False,
        "name": "Burrow",
        "objects": objects,
        "readme": readme,
        "uidata": uidata,
        "userconfigurationconfig": keyword_fields() + [
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
                "config": {"default": "off", "pairs": [["Off", "off"], ["Notify me", "notify"], ["Install automatically", "install"]]},
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


def build_swift(src, out, work=None):
    """Compile a Swift program as a universal (Apple silicon + Intel) binary."""
    if not shutil.which("swiftc"):
        sys.exit("build.py: the Swift compiler (swiftc) isn't installed. Install Apple's Command Line Tools: xcode-select --install")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = os.path.join(work or BUILD, ".swift")
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


def build_companion():
    """Burrow Companion.app (optional, separate download): menu bar icon + window.
    Written to dist/Burrow-Companion.zip."""
    if not shutil.which("swiftc"):
        sys.exit("build.py: the Swift compiler (swiftc) isn't installed. Install Apple's Command Line Tools: xcode-select --install")
    work = os.path.join(ROOT, "build-companion")
    shutil.rmtree(work, ignore_errors=True)
    app = os.path.join(work, "Burrow Companion.app")
    contents = os.path.join(app, "Contents")
    os.makedirs(os.path.join(contents, "MacOS"))
    os.makedirs(os.path.join(contents, "Resources"))
    build_swift(os.path.join(ROOT, "tools", "companion", "BurrowCompanion.swift"), os.path.join(contents, "MacOS", "Burrow Companion"), work)
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump({
            "CFBundleIdentifier": BUNDLE_ID + ".companion",
            "CFBundleName": "Burrow Companion",
            "CFBundleDisplayName": "Burrow Companion",
            "CFBundleExecutable": "Burrow Companion",
            "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,
            "NSHumanReadableCopyright": "MIT License. github.com/" + REPO,
            "CFBundleURLTypes": [{"CFBundleURLName": BUNDLE_ID + ".companion", "CFBundleURLSchemes": ["burrow-companion"]}],
        }, f)
    iconset = os.path.join(work, "AppIcon.iconset")
    os.makedirs(iconset)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = "icon_{}x{}{}.png".format(size, size, "@2x" if scale == 2 else "")
            subprocess.run(["sips", "-z", str(px), str(px), os.path.join(SRC, "icon.png"), "--out", os.path.join(iconset, name)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(contents, "Resources", "AppIcon.icns")], check=True)
    shutil.rmtree(iconset)
    subprocess.run(["codesign", "-s", "-", "--force", "--deep", app], check=True, stderr=subprocess.PIPE)
    out = os.path.join(DIST, "Burrow-Companion.zip")
    if os.path.exists(out):
        os.remove(out)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", app, out], check=True)
    print("built", out)
    return app


def main():
    if "--companion" in sys.argv:
        os.makedirs(DIST, exist_ok=True)
        app = build_companion()
        if "--install" in sys.argv:
            dest = "/Applications/Burrow Companion.app"
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(app, dest, symlinks=True)
            subprocess.run(["/usr/bin/open", dest])
            print("installed", dest)
        return
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
    os.makedirs(DIST, exist_ok=True)

    for name in ("burrow.py", "engine.py", "updates.py", "browsers.py", "run.sh"):
        shutil.copy(os.path.join(SRC, name), BUILD)
    os.chmod(os.path.join(BUILD, "run.sh"), 0o755)
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
