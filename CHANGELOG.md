# Changelog

## 1.4.0

- **Uninstaller window** in Burrow Companion: every app with its size and when it was last opened (filter by Unused or Largest), everything it installed with a checkbox to keep any item, the developer's own uninstaller and system extensions when there are any, and Uninstall, Reset and Undo. It uses the same engine as `buuninstall`, and items you keep are shared with Alfred.
- **Search fields** in the App Updates, Browsers and Uninstaller windows.
- `buuninstall` and `bumenu` open the Uninstaller window when Burrow Companion is installed.
- Burrow Companion's messages now point to `burrow` for Undo.

## 1.3.0

- **Processes** (`bukill`): see what's using CPU and memory, with live CPU measured like Activity Monitor and each app's helpers grouped under it. Quit apps normally, end or force quit any process (other users' processes ask for your password), or restart an app. Critical parts of macOS get a warning first, and a reused PID is never ended by mistake.
- **System Status:** ↩ on a top process opens it in Processes.

## 1.2.1

- **Icons:** Undo, Startup Items, Info, Check, Warning, Error and Download icons show their symbol again, instead of a plain shape.
- **Trash:** when macOS can't move an item, Burrow only asks Finder to try when that's safe. It never does for items on external or network volumes, where Finder may delete instead of moving to the Trash.
- **Automatic update checks** that are turned off now remove their login item reliably, and the old menu bar helper's login item is fully unloaded.
- **Uninstall:** apps with a 1970 file date no longer show “Modified 46 years ago”.
- **App Updates:** clearer summary when updates have to be installed from the App Store.
- **README:** screenshots, and a note that App Store and Homebrew updates are never installed automatically.

## 1.2.0

Ready for the Alfred Gallery.

- **Keywords:** the main keyword is now `burrow`, and every keyword can be changed in the Workflow’s Configuration.
- **Nothing is installed for you any more.** When Apple's Command Line Tools or mas are missing, Burrow shows the command to install them.
- **No compiled code in the workflow.** Moving to the Trash now uses macOS directly from Python, and Undo works as before.
- **Burrow Companion:** the menu bar health score and the Updates and Browsers windows are now a separate, optional app (`Burrow-Companion.zip` in the release). `bumenu` opens it, or links to the download. The old menu bar helper and its login item are removed automatically.
- **Updates:** Burrow no longer updates itself; Alfred Gallery handles that. Automatic update checks are now off until you turn them on in the Workflow’s Configuration.
- **App Store updates** install from Burrow with mas, with one password prompt.
- **Undo and rollback:**
  - Rolling back an update now works for apps in system-owned folders.
  - A cancelled password prompt keeps the rollback so you can retry.
  - Anything already open is quit first (after asking).
  - A rollback can itself be undone.
- **Update rollbacks stay available** even after lots of cleaning.
- **Daily auto-install never quits** an app you've just opened.
- **Clear the Cache of Every Closed Browser** is now one Undo step.
- **Browsers:** Safari is detected as open, database backups include recent data still being written, Orion RC is supported, a deleted profile no longer blocks the browser view, and `bubrowsers` is faster.
- **Offline:** `burrow` and `buupdates` never wait for the network while you type, and a failed check says so.
- **Update check:** no longer offers an unrelated app's update when two apps share a name (Helium browser).
## 1.1.0

- **Burrow window** with two sections:
  - **App updates:** every update with release notes, Update or Update all with progress, skip or ignore, and rolling back past updates.
  - **Browsers:** switches for everything you can clean.

  Open it from `buupdates`, `bubrowsers` or the menu bar.
- **Browser cleaning and reset** (`bubrowsers`) for every browser on the Mac, found by how it stores data:
  - **Chromium:** Chrome, Brave, Edge, Opera, Vivaldi, Arc, Dia, Comet, Helium, Sigma…
  - **Firefox:** Firefox, LibreWolf, Floorp, Zen, Tor, Waterfox…
  - **Also:** Safari and Orion.

  Clean the cache, browsing history, download history, cookies and site data, open tabs, form data and (with a second confirmation) saved passwords, for the last hour, day, week, 4 weeks or all time. Reset settings keeps bookmarks, history and passwords; a full reset starts the profile fresh. Everything can be undone.
- **Clear the Cache of Every Closed Browser.**
- **Clean** skips anything in use: open files, running apps and their background helpers, and developer tools while they run. It checks again right before cleaning.
- **Status** numbers now match macOS: named core types, Finder's available space, the Mac's own power draw, and battery condition.
- **Fixed:** the Optimize font-cache and Launch Services tasks on recent macOS.

## 1.0.1

- Fixed: the daily update check and the command-line engine couldn't save results if Alfred hadn't created Burrow's cache folder yet. The error was swallowed, so the check silently found nothing.

## 1.0.0

The first release.

- **System Status** (`bustatus`) and a menu bar health score (`bumenu`): health, CPU, GPU, memory, disks, battery, temperatures, fans, power and network.
- **Clean** (`buclean`): moves app, browser, developer and system caches, logs and old temporary files to the Trash. Anything in use is skipped.
- **Uninstall** (`buuninstall`): a complete uninstall you can review first, **Reset**, and leftovers of deleted apps.
- **App Updates** (`buupdates`): checks the App Store, Homebrew, Sparkle, Electron and Homebrew's catalog, and installs verified updates. It can check daily.
- **Analyze Disk**, **Large Files**, **Duplicates**, **Purge Dev Artifacts**, **Clean Installers**, **Startup Items**, **Optimize** and **Touch ID for sudo**.
- **One-step Undo** for everything Burrow moves to the Trash, including rolling back an app update.
- **Burrow updates itself** from GitHub releases.
