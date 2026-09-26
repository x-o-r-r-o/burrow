#!/bin/bash
# Burrow launcher. Every Alfred object calls this instead of Python directly, so a
# Mac without Python gets a clear explanation instead of an error. Burrow uses the
# /usr/bin/python3 that comes with Apple's Command Line Tools; it never installs
# anything itself.
#
#   run.sh <command> [query]

cd "$(dirname "$0")" || exit 1
command="$1"
query="$2"

cache="${alfred_workflow_cache:-$HOME/Library/Caches/com.runningwithcrayons.Alfred/Workflow Data/${alfred_workflow_bundleid:-io.github.burrow-alfred}}"
[ -d "$cache" ] || mkdir -p "$cache"

python_ready() {
  # Fast path: the usual Command Line Tools location, without spawning xcode-select.
  [ -x /Library/Developer/CommandLineTools/usr/bin/python3 ] && return 0
  local dev
  dev="$(/usr/bin/xcode-select -p 2>/dev/null)" || return 1
  [ -x "$dev/usr/bin/python3" ]
}

if python_ready; then
  # Importing burrow (rather than running it as a script) lets Python cache its
  # compiled bytecode, which makes every keystroke a little faster. The cache
  # lives in Alfred's cache folder, not the (often synced) workflow folder.
  export PYTHONPYCACHEPREFIX="$cache/pycache"
  exec /usr/bin/python3 -c 'import sys; sys.path.insert(0, "."); import burrow; burrow.main()' "$command" "$query"
fi

# --- Python is missing: explain how to get it -------------------------------

install_command="xcode-select --install"

if [ "$command" = "run" ]; then
  printf '%s' "$install_command" | /usr/bin/pbcopy
  echo "Copied “$install_command”. Paste it in Terminal to install Apple's Command Line Tools."
  exit 0
fi

icon="$PWD/icons/download.png"
cat <<JSON
{"items": [
  {"title": "Burrow Needs Apple's Command Line Tools",
   "subtitle": "They include the Python Burrow runs on · ↩ Copy the install command ($install_command)",
   "icon": {"path": "$icon"}, "arg": "", "variables": {"action": "setup"}},
  {"title": "Then Open Burrow Again",
   "subtitle": "Paste the command in Terminal, click Install in Apple's window, and wait for it to finish",
   "icon": {"path": "$icon"}, "valid": false}
]}
JSON
