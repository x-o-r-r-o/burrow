# Burrow

Dig out the clutter. Burrow deep cleans and optimizes your Mac from Alfred: it frees disk space, removes apps completely, finds what's taking up room, and shows how your Mac is doing.

Everything runs on Burrow's own engine, so you don't need Homebrew or any other tools. The one requirement, Apple's Command Line Tools, is installed for you the first time you run Burrow if your Mac doesn't have it.

## Install

1. Download **Burrow.alfredworkflow** from the [latest release](https://github.com/x-o-r-r-o/burrow/releases/latest).
2. Double-click it (Alfred 5 with the Powerpack).
3. Type `bu`.

Burrow updates itself. When a new release is out, `bu` shows **Burrow X Is Available**. Burrow checks the download against the release's SHA-256 checksum, then hands it to Alfred, and Alfred asks you to confirm.

That's it. Burrow uses the Python that comes with Apple's Command Line Tools. If your Mac doesn't have them yet, the first time you open Burrow it opens Apple's installer for you. Click **Install**, or choose **Install Automatically** in Alfred to install them without Apple's window. That option asks for your password once. Burrow checks every couple of seconds and starts working as soon as the install finishes.

## Commands

| Keyword        | Command             | What it does                                                                                       |
| -------------- | ------------------- | -------------------------------------------------------------------------------------------------- |
| `bu`           | Burrow              | Lists every command, plus Undo, Empty Trash and how much space Burrow has freed                    |
| `buupdates`    | App Updates         | Checks every app for a newer version (App Store, Homebrew, Sparkle, Electron, Homebrew's catalog) and installs updates after verifying them |
| `bubrowsers`   | Browsers            | Clears history, cache, cookies, tabs, form data or passwords (by time range) in every browser, or resets it |
| `bustatus`     | System Status       | Live health score, CPU, GPU, temperatures, fans, memory, disks, battery, power, network and top processes |
| `buclean`      | Clean System        | Finds app, browser, developer and system caches, logs, crash reports and old temporary files, and moves them to the Trash |
| `buoptimize`   | Optimize System     | Flushes DNS, frees memory, rebuilds Launch Services, resets Quick Look and font caches, and more. Developer extras (Homebrew cleanup, Docker prune, unused simulators) appear when those tools are installed |
| `buuninstall`  | Uninstall App       | ↩ reviews an app before removing it: every leftover (with 🔒 on system-owned ones), system extensions, and the developer's own uninstaller if it ships one. **Uninstall Completely** or **Reset** (keep the app, wipe its settings). Type `unused`, `big` or `leftovers` |
| `buanalyze`    | Analyze Disk        | Browses folders sorted by size (`buanalyze ~/Library/`)                                            |
| `bularge`      | Large Files         | Files over 500 MB in your home folder with when they were last opened. Filter with `2gb`, `video`, `audio`, `image`, `archive`, `disk`, `old` |
| `budupes`      | Duplicate Files     | Finds identical copies in Downloads, Desktop, Documents, Movies, Music and Pictures (or any folder you type). Keep the newest with ⌘↩, or pick copies yourself |
| `bustartup`    | Startup Items       | Lists launch agents and daemons, and removes ones left behind by apps you've deleted              |
| `bupurge`      | Purge Dev Artifacts | Finds `node_modules`, `.next`, `dist`, `target`, `venv`, `Pods` and similar folders in projects you haven't touched lately |
| `buinstallers` | Clean Installers    | Finds `.dmg`, `.pkg` and `.iso` files in Downloads, Desktop and Documents                          |
| `butouchid`    | Touch ID for Sudo   | Turns fingerprint authentication for `sudo` on or off                                              |
| `bumenu`       | Menu Bar Health     | Shows the health score in the menu bar, with an optional start at login                            |

### Keys

The same key means the same thing wherever a row supports it:

- **↩** runs the row's action. Anything that removes files asks you to confirm first.
- **⌘↩** reveals the item in Finder.
- **⌥↩** moves the item to the Trash (in Uninstall: uninstall straight away; in Duplicates: keep the newest copy).
- **⌃↩** copies the path (in Clean: never clean this item).
- **⌘C** copies, **⌘L** shows details in Large Type, **⇧** opens Quick Look.
- Actions on everything (Clean all, Purge all, Keep newest of each…) live on the summary row at the top.
- In Analyze and Duplicates, **⇥** or **↩** on a folder opens it. Type after the trailing `/` to filter.

### From Alfred's file actions

Select an app or folder in Alfred's file search and press **→**:

- **Uninstall with Burrow** (apps)
- **Analyze with Burrow** and **Find Duplicates with Burrow** (folders)

## What a complete uninstall removes

- **The app.** It's quit first, and force-quit only if you agree. It's also taken out of the Dock.
- **Background items:** its launch agents and system daemons are stopped and removed.
- **Settings and data:** Preferences, Application Support, Caches, Containers, Saved State, HTTP storage, WebKit data, cookies and logs.
- **Shared data folders the app declares in its signature.** Folders shared with the developer's other installed apps are kept, so uninstalling Word never touches Excel's data.
- **Hidden caches:** the app's per-user cache and temp folders under `/var/folders`.
- **Crash reports** named after the app.
- **What its installer added:** helper tools, plug-ins and drivers installed outside the app, plus the installer receipts.
- **Add-ons:** audio plug-ins, Quick Look and Spotlight plug-ins, screen savers and preference panes.
- **Homebrew's record** of the app, if it came from `brew install --cask`.

System-owned items need your password. macOS asks once for all of them. The whole uninstall is one **Undo** step.

Apps with **system extensions** (VPNs, firewalls, antivirus, drivers) are flagged. macOS only removes those from **System Settings → Login Items & Extensions**, or with the developer's uninstaller, which Burrow shows when the app ships one.

## Browsers

`bubrowsers` finds every browser on your Mac by how it stores its data, so browsers that aren't listed here work too:

| Engine | Browsers |
|---|---|
| Chromium | Chrome, Brave, Edge, Opera, Vivaldi, Arc, Dia, Comet, Helium, Sigma, Chromium, Yandex, Thorium |
| Firefox | Firefox, LibreWolf, Floorp, Zen, Tor Browser, Waterfox, Mullvad |
| WebKit | Safari, Orion |

Pick a browser, switch on what to clean, and choose a time range (last hour, day, week, 4 weeks or all time):

- **Cache**
- **Browsing history.** Bookmarks are never touched, even in Firefox, which keeps them in the same file.
- **Download history.** The downloaded files stay.
- **Cookies and site data.** This signs you out of websites. Site storage can only be cleared for all time.
- **Open tabs and sessions**
- **Saved form data.** Addresses and cards are kept.
- **Saved passwords.** Removing them asks you to confirm a second time.

To reset a browser:

- **Reset settings** restores its defaults. Bookmarks, history and passwords stay.
  - **Chromium browsers:** extensions *and their stored data* (a wallet or password-manager extension's local vault, for example) move to the Trash with the old settings. Undo brings them back.
  - **Firefox browsers:** extensions stay.
- **Full reset** moves everything to the Trash, as if the browser had just been installed, including bookmarks and passwords. It covers the chosen profile, or all profiles if you haven't picked one.

**How it keeps your data safe:**

- The browser is quit first, if you agree.
- Files go to the Trash.
- Databases that are only partly cleaned get a backup in the Trash first. Undo restores them.
- If you use sync, anything you delete may come back from your account.

Cache and open tabs are always cleared completely; the time range applies to the other categories.

Keys in `bubrowsers`:
- On a browser row: **↩** choose what to clean, **⌥↩** clean now with your saved choices, **⌘↩** reveal its data folder, **⌃↩** copy the folder's path.
- Inside a browser: **↩** turns a category on or off, and on **Time Range**, **⌥↩** goes back one step.

**Clear the Cache of Every Closed Browser** clears every closed browser's cache as one Undo step. Choices you make in Alfred and in the window are shared.

**Safari:** macOS protects Safari's data, so Alfred needs **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access). Burrow shows a button that opens that page. Safari keeps passwords and form data in the Passwords app, so Burrow leaves those alone.

**The Burrow window** (open it from `buupdates`, `bubrowsers` or the menu bar) shows App Updates and Browsers with switches, progress bars and release notes.

## App updates

`buupdates` checks your apps against the same sources the apps themselves use:

| Source | Which apps | How the update is installed |
|---|---|---|
| **Mac App Store** | Apps from the App Store | Updated from Burrow with [mas](https://github.com/mas-cli/mas), with one password prompt. Burrow installs mas for you with Homebrew when you click **Install mas**. Without it, and for iPhone/iPad apps, the App Store opens instead |
| **Homebrew** | Apps you installed with `brew install --cask` | `brew upgrade --cask` |
| **Sparkle** | Apps with a built-in "Check for Updates…" (a Sparkle feed) | Burrow downloads and installs it |
| **Electron** | Apps using electron-updater (GitHub releases or a feed) | Burrow downloads and installs it |
| **Homebrew catalog** | Everything else Homebrew knows (about 7,000 apps), even if you didn't install it with brew | Burrow downloads and installs it |

Before Burrow installs anything, the download must pass every check:

- It's served over HTTPS, and matches Homebrew's checksum when the catalog has one.
- It contains the **same app**, by bundle ID.
- It has a **valid Apple code signature** from the **same developer** (Team ID) as the version you have.
- If your current version passes **Gatekeeper**, the new one must pass too.
- It's actually **newer**.

If any check fails, nothing is changed. The old version goes to the Trash, so **Undo rolls the update back**. An app that's open is quit first and reopened afterwards.

Keys: ↩ update · ⌘↩ release notes · ⌥↩ skip this version · ⌃↩ never check this app.

**Automatic checks** (Configure Workflow → Automatic Update Checks) run once a day:
- **Notify me** (the default) tells you what's new.
- **Install automatically** also installs verified updates for apps that aren't open. It skips App Store and Homebrew updates: those need a password or Homebrew, and macOS's own App Store updates cover App Store apps.
- **Off** turns daily checks off.

Apps with their own updaters (Microsoft, Google Chrome, Adobe, JetBrains, Setapp) are left to them.

## How it keeps you safe

- **Nothing is deleted outright.** Everything Burrow removes goes to the Trash. Use **Empty Trash** (in `bu` or at the bottom of `buclean`) when you're ready to free the space.
- **One-step undo.** `bu` shows **Undo** for the last thing Burrow moved to the Trash (a clean, an uninstall, a duplicate…) and puts it all back where it was.
- **You decide what stays.** ⌃↩ on a clean item marks it “never clean”; ↩ on an uninstall leftover keeps it.
- **Optional items stay optional.** iPhone update files, device backups and Xcode archives are listed in `buclean` but never included in **Clean all**.
- **Caches of running apps are skipped**, because removing them can upset the app. Burrow lists which apps it skipped. Quit them and rescan to include their caches.
- **System caches are left alone.** Anything owned by macOS (`com.apple.*`), plus protected folders like Mail, Messages and Safari data.
- **Build folders need proof.** `node_modules` always counts. `dist`, `.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.parcel-cache`, `.angular` and `.output` need a `package.json`. `build` needs a `package.json` or Gradle file. `target` needs `Cargo.toml` or `pom.xml`. `venv`/`.venv`/`env` need `pyvenv.cfg`. `Pods` needs a `Podfile`, and `.gradle` needs a Gradle file.
- **Admin tasks use the standard macOS password prompt,** and each one asks only once. That covers flushing DNS, freeing memory, rebuilding Spotlight, Touch ID for sudo and cleaning system caches.

## Configuration

Open the workflow in Alfred Preferences and click **Configure Workflow…**:

| Setting              | Description                                                                         |
| -------------------- | ----------------------------------------------------------------------------------- |
| Status Refresh       | How often System Status refreshes: 1, 3 or 5 s                                      |
| Automatic Update Checks | Daily update check: Off, Notify me (default) or Install automatically   |
| Menu Bar Refresh     | How often the menu bar health score updates: 10 s to 5 min                         |
| Analyze Default Path | The folder `buanalyze` opens when you don't type a path                             |
| Project Folders      | Folders Purge searches, comma-separated. By default it checks `~/Projects`, `~/Developer`, `~/Code`, `~/Documents`, `~/Desktop` and other common places |
| Large File Size      | The size Large Files starts at (100 MB to 5 GB)                                     |
| Purge Minimum Age    | Only list build folders in projects untouched for at least this long (default 1 week) |

## Good to know

- **Offline:** update checks use the last results, and `bu` never waits for the network while you type.
- Scans run in the background and results fill in as they're found. Results are reused for 10 minutes (Analyze: 15), so reopening is instant. Use **Rescan** for fresh results.
- The first **Empty Trash** asks you to let Alfred control Finder.
- Temperatures and fan speeds come straight from the Mac's hardware sensor chip (the SMC), without admin rights.
- The menu bar indicator is a small helper app inside the workflow (`bin/BurrowMenu`, macOS 13 or later). Its menu opens Burrow commands in Alfred.

## Command line

The engine works without Alfred too. Run it from the installed workflow folder, so it can use the bundled Trash helper (that helper is what makes Undo possible):

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

## Other ways in

- **The menu bar:** its menu opens Burrow commands through the URL `alfred://runtrigger/io.github.burrow-alfred/open/?argument=clean`. Any command name works (`status`, `clean`, `analyze`…), so you can use it from other apps and scripts too.
- **Undo** puts back the most recent thing Burrow moved to the Trash, and then the one before that.
- **Renaming keywords:** you can rename any keyword in Alfred Preferences. Burrow reads the new names, so moving between commands still works.

## Development

```bash
python3 build.py --install                  # package dist/Burrow.alfredworkflow and open it in Alfred
/usr/bin/python3 -m unittest discover tests # run the tests
swift tools/render_icons.swift src/icons    # regenerate the SF Symbol icons
swift tools/render_logo.swift src/icon.png 512  # redraw the Burrow logo
```

Building needs the Swift compiler from Apple's Command Line Tools. It compiles the menu bar helper as a universal (Apple silicon and Intel) binary.

- `src/engine.py` does the scanning and maintenance work.
- `src/burrow.py` builds what Alfred shows.
- `src/updates.py` checks for and installs app updates.
- `src/browsers.py` finds browsers, and cleans or resets them.
- `tools/window/BurrowWindow.swift` is the Burrow window.
- `src/run.sh` is the launcher that sets up Python when it's missing.
- `tools/menubar/BurrowMenu.swift` is the menu bar helper.
- `tools/trash/BurrowTrash.swift` moves files to the Trash and reports where they went, so they can be put back.

## Releasing

1. Bump `VERSION`.
2. Add a `## <version>` section to `CHANGELOG.md`.
3. Commit.
4. Run `python3 release.py`.

The script runs the tests, builds the workflow, and writes its SHA-256. It then tags the release, pushes, and publishes the GitHub release.

## License

MIT, see [LICENSE](LICENSE).

## Credits

The workflow's design is ported from the Raycast "Mole" extension by jlrochin and adria_navarrete (MIT License).
