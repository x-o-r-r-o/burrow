# Developing Burrow

## Layout

- `src/run.sh` is the launcher every Alfred object calls. It explains how to install Apple's Command Line Tools when Python is missing, and never installs anything itself.
- `src/burrow.py` builds what Alfred shows and runs the actions.
- `src/engine.py` does the scanning and maintenance work, and moves files to the Trash (through `NSFileManager`, so every move can be undone).
- `src/updates.py` checks for and installs app updates.
- `src/browsers.py` finds browsers, and cleans or resets them.
- `src/uninstaller.py` is the command line Burrow Companion's Uninstaller window uses.
- `tools/companion/BurrowCompanion.swift` is Burrow Companion, the optional menu bar app and window. It's a separate download, not part of the workflow.
- `build.py` generates `info.plist` and packages the workflow.

## Build and test

```bash
python3 build.py --install                  # package dist/Burrow.alfredworkflow and open it in Alfred
/usr/bin/python3 -m unittest discover tests # run the tests (build first)
python3 build.py --companion --install      # build Burrow Companion and copy it to /Applications
swift tools/render_icons.swift src/icons    # regenerate the SF Symbol icons
swift tools/render_logo.swift src/icon.png 512  # redraw the Burrow logo
```

The workflow itself contains no compiled code. Building Burrow Companion needs the Swift compiler from Apple's Command Line Tools; it's built as a universal app and signed ad hoc.

## Command line

The engine works without Alfred:

```bash
/usr/bin/python3 engine.py status            # system status as JSON
/usr/bin/python3 engine.py clean-scan        # what Clean would find
/usr/bin/python3 engine.py purge-scan [DAYS] # build folders older than DAYS
/usr/bin/python3 engine.py analyze ~/Library # folder sizes
/usr/bin/python3 engine.py large [BYTES]     # big files (default 500 MB)
/usr/bin/python3 engine.py dupes [FOLDER…]   # identical files
/usr/bin/python3 engine.py leftovers         # files from deleted apps
/usr/bin/python3 engine.py startup           # launch agents and daemons
/usr/bin/python3 engine.py optimize [ID…]    # list tasks, or run the ones named
/usr/bin/python3 engine.py touchid [status|enable|disable]
/usr/bin/python3 engine.py trash PATH…       # move to the Trash
```

## Releasing

1. Bump `VERSION`.
2. Add a `## <version>` section to `CHANGELOG.md`.
3. Commit.
4. Run `python3 release.py`.

The script runs the tests, and builds the workflow and Burrow Companion. It writes `SHA256SUMS`, tags the release, pushes, and publishes the GitHub release with all three files.
