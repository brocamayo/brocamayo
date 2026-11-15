#!/bin/bash
# Turn on hands-free posting: every 5 minutes, look for new *-packaging.md files in ~/Downloads
# (and ~/social-scheduler/inbox), have Claude Code check and queue them, and send the summary
# to your phone. The regular socialq job then posts them.
#   cd ~/social-scheduler && bash setup-intake-mac.sh
# To turn it off:  launchctl bootout gui/$(id -u)/com.socialq.intake

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
LABEL="com.socialq.intake"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SEARCH_PATH="$HOME/.local/bin:$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

[ -x .venv/bin/python ] || { echo "Run setup-mac.sh first."; exit 1; }

CLAUDE="$(PATH="$SEARCH_PATH:$PATH" command -v claude || true)"
if [ -z "$CLAUDE" ]; then
  echo "Claude Code isn't installed yet. Install it, log in once, then run this again:"
  echo
  echo "    curl -fsSL https://claude.ai/install.sh | bash"
  echo "    claude        # log in, then type /exit"
  echo
  exit 1
fi
echo "Using Claude Code: $CLAUDE"

mkdir -p inbox intake/specs
# Packaging files already in Downloads were handled by hand; only pick up new ones.
./sq intake --mark-seen

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string>
    <string>-m</string>
    <string>socialq</string>
    <string>intake</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$CLAUDE"):$SEARCH_PATH</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$ROOT/intake.log</string>
  <key>StandardErrorPath</key><string>$ROOT/intake.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Registered '$LABEL' (checks for new packages every 5 minutes)."

PY_REAL="$(.venv/bin/python -c 'import os, sys; print(os.path.realpath(sys.executable))')"
PY_APP="$(.venv/bin/python -c 'import os, sys; p = os.path.join(sys.base_prefix, "Resources", "Python.app"); print(p if os.path.isdir(p) else "")')"
CLAUDE_REAL="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$CLAUDE")"

cat <<EOF

== One last step: let the background job read Downloads ==

macOS blocks background jobs from Downloads. Opening the settings page now:
System Settings → Privacy & Security → Full Disk Access → click + , press ⌘⇧G,
paste each path below, and click Open:

    $PY_REAL
EOF
[ -n "$PY_APP" ] && echo "    $PY_APP"
cat <<EOF
    $CLAUDE_REAL

If you skip this, your phone gets a "can't read your Downloads" notification.

Test it now (runs once, in this window):
    ./sq intake -v
EOF
open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
