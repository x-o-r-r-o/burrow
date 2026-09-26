#!/usr/bin/python3
"""Publish a Burrow release on GitHub.

    1. Bump VERSION and add a "## <version>" section to CHANGELOG.md
    2. Commit everything
    3. python3 release.py

Runs the tests, builds dist/Burrow.alfredworkflow and the optional
dist/Burrow-Companion.zip, writes their SHA-256 checksums, tags v<version>, pushes,
and creates the GitHub release with the files attached and the changelog section
as release notes.
"""

import hashlib
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def run(*args, **kw):
    return subprocess.run(args, cwd=ROOT, check=True, **kw)


def main():
    with open(os.path.join(ROOT, "VERSION")) as f:
        version = f.read().strip()
    tag = "v" + version
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
        sys.exit("release.py: commit your changes first")
    if subprocess.run(["git", "rev-parse", tag], cwd=ROOT, capture_output=True).returncode == 0:
        sys.exit("release.py: {} already exists; bump VERSION".format(tag))
    with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
        m = re.search(r"^## {}\s*\n(.*?)(?=^## |\Z)".format(re.escape(version)), f.read(), re.S | re.M)
    if not m:
        sys.exit("release.py: add a '## {}' section to CHANGELOG.md".format(version))

    run("/usr/bin/python3", "-m", "unittest", "discover", "tests")
    run("/usr/bin/python3", "build.py")
    run("/usr/bin/python3", "build.py", "--companion")
    assets = [os.path.join(ROOT, "dist", name) for name in ("Burrow.alfredworkflow", "Burrow-Companion.zip")]
    sums = os.path.join(ROOT, "dist", "SHA256SUMS")
    with open(sums, "w") as f:
        for asset in assets:
            f.write("{}  {}\n".format(hashlib.sha256(open(asset, "rb").read()).hexdigest(), os.path.basename(asset)))
    notes = os.path.join(ROOT, "dist", "notes.md")
    with open(notes, "w") as f:
        f.write(m.group(1).strip() + "\n\n**Install:** download `Burrow.alfredworkflow` and double-click it.\n\n"
                "**Optional:** `Burrow-Companion.zip` adds the health score to the menu bar and the Updates and "
                "Browsers windows. It isn't notarized; see the README for how to open it.\n")

    run("git", "tag", "-a", tag, "-m", "Burrow " + version)
    run("git", "push", "origin", "HEAD", tag)
    run("gh", "release", "create", tag, *assets, sums, "--title", "Burrow " + version, "--notes-file", notes)
    print("Released", tag)


if __name__ == "__main__":
    main()
