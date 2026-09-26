#!/bin/bash
# Burrow launcher. Every Alfred object calls this instead of Python directly so
# that a Mac without Python gets set up automatically: Burrow only needs the
# /usr/bin/python3 that ships with Apple's Command Line Tools.
#
#   run.sh <command> [query]

cd "$(dirname "$0")" || exit 1
command="$1"
query="$2"

cache="${alfred_workflow_cache:-$HOME/Library/Caches/com.runningwithcrayons.Alfred/Workflow Data/${alfred_workflow_bundleid:-io.github.burrow-alfred}}"
[ -d "$cache" ] || mkdir -p "$cache"
marker="$cache/clt-install-started"

python_ready() {
  # Fast path: the usual Command Line Tools location, without spawning xcode-select.
  [ -x /Library/Developer/CommandLineTools/usr/bin/python3 ] && return 0
  local dev
  dev="$(/usr/bin/xcode-select -p 2>/dev/null)" || return 1
  [ -x "$dev/usr/bin/python3" ] || [ -x "/Library/Developer/CommandLineTools/usr/bin/python3" ]
}

# A downloaded workflow carries macOS's quarantine flag, which would block the
# bundled helpers (menu bar app, trash helper). Clear it once per build: every
# build writes a unique stamp, and the marker remembers the last one handled.
stamp=""; done_stamp=""
[ -f bin/.stamp ] && read -r stamp < bin/.stamp
[ -f "$cache/helpers-ready" ] && read -r done_stamp < "$cache/helpers-ready"
if [ -d bin ] && [ "$stamp" != "$done_stamp" -o -z "$stamp" ]; then
  /usr/bin/xattr -dr com.apple.quarantine bin 2>/dev/null
  chmod +x bin/* 2>/dev/null
  echo "$stamp" > "$cache/helpers-ready"
fi

if python_ready; then
  [ -f "$marker" ] && rm -f "$marker"
  # Importing burrow (rather than running it as a script) lets Python cache its
  # compiled bytecode, which makes every keystroke a little faster. The cache
  # lives in Alfred's cache folder, not the (often synced) workflow folder.
  export PYTHONPYCACHEPREFIX="$cache/pycache"
  exec /usr/bin/python3 -c 'import sys; sys.path.insert(0, "."); import burrow; burrow.main()' "$command" "$query"
fi

# --- Python is missing: install Apple's Command Line Tools -------------------

# Installs the newest Command Line Tools through Software Update with a single
# password prompt, the same way Homebrew's installer does it.
notify() {
  /usr/bin/osascript -e 'on run argv' \
    -e 'tell application id "com.runningwithcrayons.Alfred" to run trigger "notify" in workflow (item 1 of argv) with argument (item 2 of argv)' \
    -e 'end run' "${alfred_workflow_bundleid:-io.github.burrow-alfred}" "$1" >/dev/null 2>&1
}

apple_installer_open() {
  /usr/bin/pgrep -q "Install Command Line Developer Tools"
}

# Returns 0 installed, 2 cancelled at the password prompt, 1 failed.
silent_install() {
  out=$(/usr/bin/osascript \
    -e 'on run argv' \
    -e 'do shell script (item 1 of argv) with prompt "Burrow needs to install Apple’s Command Line Tools (it provides Python)." with administrator privileges' \
    -e 'end run' \
    'touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
label=$(softwareupdate -l 2>/dev/null | grep -E "\* Label: Command Line Tools" | sed -E "s/^.*Label: //" | sort -V | tail -n1)
if [ -n "$label" ]; then softwareupdate -i "$label" --verbose; status=$?; else status=1; fi
rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
exit $status' 2>&1)
  status=$?
  case "$out" in *-128*) return 2 ;; esac
  return $status
}

if [ "$command" = "run" ]; then
  if [ "$action" = "setup_silent" ]; then
    # One install at a time: Apple's window and Software Update would collide.
    if apple_installer_open; then
      echo "Apple's installer is already open. Finish the install there."
      exit 0
    fi
    notify "Installing Apple's Command Line Tools. This takes a few minutes…"
    silent_install
    case $? in
      0) echo "Command Line Tools installed. Burrow is ready." ;;
      2) : ;;  # cancelled at the password prompt: say nothing
      *) echo "The automatic install didn't work. Choose “Open Apple's Installer” in Burrow to install it the usual way." ;;
    esac
  else
    if /usr/bin/xcode-select --install >/dev/null 2>&1; then
      echo "Click Install in Apple's window to finish setting up Burrow."
    elif apple_installer_open; then
      echo "Apple's installer is already open. Finish the install there."
    else
      echo "Apple's installer couldn't open. Choose “Install Automatically” in Burrow instead."
    fi
  fi
  exit 0
fi

# First time we notice, open Apple's installer straight away.
if [ ! -f "$marker" ]; then
  touch "$marker"
  /usr/bin/xcode-select --install >/dev/null 2>&1 &
fi

icon="$PWD/icons/download.png"
cat <<JSON
{"rerun": 2, "items": [
  {"title": "Setting Up Burrow…",
   "subtitle": "Burrow needs Apple's Command Line Tools. Click Install in Apple's window",
   "icon": {"path": "$icon"}, "valid": false},
  {"title": "Open Apple's Installer",
   "subtitle": "If the install window was closed",
   "icon": {"path": "$icon"}, "arg": "", "variables": {"action": "setup"}},
  {"title": "Install Automatically",
   "subtitle": "Without Apple's window · asks for your password · takes a few minutes",
   "icon": {"path": "$icon"}, "arg": "", "variables": {"action": "setup_silent"}}
]}
JSON
