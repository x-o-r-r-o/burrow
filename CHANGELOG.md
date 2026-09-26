# Changelog

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
