#!/bin/bash
# One-click macOS setup for socialq.
#   cd ~/social-scheduler && bash setup-mac.sh
#
# Installs dependencies, creates your config files, and registers a
# background job (launchd) that checks the queue every 10 minutes.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
LABEL="com.socialq.run"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo
echo "== socialq setup =="
echo

# macOS blocks background jobs from reading Desktop/Documents/Downloads.
case "$ROOT" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "This folder is inside Desktop/Documents/Downloads, which macOS won't let"
    echo "background jobs read. Move it to your home folder first, e.g.:"
    echo
    echo "    mv \"$ROOT\" ~/social-scheduler && cd ~/social-scheduler && bash setup-mac.sh"
    echo
    exit 1
    ;;
esac

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "Python 3.10+ is needed. Install it with Homebrew (https://brew.sh):"
  echo "    brew install python"
  echo "or from https://www.python.org/downloads/ , then run this script again."
  exit 1
fi
echo "Using $($PYTHON --version)"

[ -d .venv ] || "$PYTHON" -m venv .venv
echo "Installing dependencies..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

echo "Creating config files..."
.venv/bin/python -m socialq init "$ROOT"

# Shortcut so you can type ./sq instead of the full python path.
cat > sq <<EOF
#!/bin/bash
cd "$ROOT" && SOCIALQ_CMD=./sq exec .venv/bin/python -m socialq "\$@"
EOF
chmod +x sq

# Background job: every 10 minutes, and once at login.
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
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$ROOT/launchd.log</string>
  <key>StandardErrorPath</key><string>$ROOT/launchd.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Registered background job '$LABEL' (runs every 10 minutes)."

cat <<EOF

== Done ==

Next steps (see README.md > Platform setup):
  1. Put your YouTube login file here:
       $ROOT/credentials/youtube_client_secret.json
  2. Fill in the developer-app keys in .env (opening it now)
  3. See which logins are still needed (it prints the exact commands):
       ./sq accounts
     e.g. ./sq auth youtube --account main
  4. Queue a video:
       ./sq add videos/my-video.mp4 --title "..." --caption "..." --tags a,b

To stop auto-posting:  launchctl bootout gui/\$(id -u)/$LABEL
EOF
open -e "$ROOT/.env" 2>/dev/null || true
