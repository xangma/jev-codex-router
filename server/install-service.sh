#!/bin/zsh
# Install (or re-install) the Jev Router launchd service.
# Re-run after source updates. If launchctl is restricted, use the user's Terminal.
#
#   bash ~/Documents/Github/jev-codex-router/server/install-service.sh
#
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
node "$REPO/router/src/service-write-guard.mjs" --live-install
RUNTIME="$HOME/.local/share/jev-codex-router-runtime"
PYTHON="${JEV_PYTHON:-$(command -v python3 || true)}"
LABEL="${JEV_ROUTER_LABEL:-com.thibaultsaintjean.jev-router}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs"
CODEX_HOME_VALUE="${CODEX_HOME:-$HOME/.codex}"
STATE_VALUE="${MODEL_ROUTER_STATE_DIR:-${CODEX_ROUTER_STATE_DIR:-${KIMI_CODEX_STATE_DIR:-$CODEX_HOME_VALUE/codex-router}}}"
saved_value() {
  [ -f "$PLIST" ] || return 0
  local saved
  if saved=$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$1" "$PLIST" 2>/dev/null); then
    printf '%s' "$saved"
  fi
}
JEV_API_KEY_FILE_VALUE="${JEV_API_KEY_FILE-$(saved_value JEV_API_KEY_FILE)}"
JEV_ENV_FILE_VALUE="${JEV_ENV_FILE-$(saved_value JEV_ENV_FILE)}"
DECISION_PROVIDER_VALUE="${JEV_DECISION_PROVIDER-$(saved_value JEV_DECISION_PROVIDER)}"

xml_escape() {
  printf '%s' "$1" |
    sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
      -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"
}

CODEX_HOME_XML="$(xml_escape "$CODEX_HOME_VALUE")"
STATE_XML="$(xml_escape "$STATE_VALUE")"
DECISION_ENV_XML="$(
  for name in JEV_DECISION_PROVIDER CLEF_ENDPOINT CLEF_MODEL CLEF_API_KEY_FILE CLEF_EFFORT_BOUNDS_FILE JEV_DECISION_TIMEOUT JEV_BASELINE_MODEL; do
    value=$(printenv "$name" 2>/dev/null || saved_value "$name")
    if [ -n "$value" ]; then
      printf '    <key>%s</key><string>%s</string>\n' "$name" "$(xml_escape "$value")"
    fi
  done
)"
JEV_API_KEY_FILE_XML=""
if [ "$DECISION_PROVIDER_VALUE" != clef ] && [ -n "$JEV_API_KEY_FILE_VALUE" ]; then
  [ -r "$JEV_API_KEY_FILE_VALUE" ] || { echo "Jev API key file is not readable" >&2; exit 1; }
  JEV_API_KEY_FILE_XML="$(xml_escape "$JEV_API_KEY_FILE_VALUE")"
fi
JEV_ENV_XML=""
if [ -n "$JEV_ENV_FILE_VALUE" ]; then
  JEV_ENV_XML="$(xml_escape "$JEV_ENV_FILE_VALUE")"
fi

[ -x "$PYTHON" ] || { echo "Python 3.11+ not found; set JEV_PYTHON to its executable path." >&2; exit 1; }
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 or newer is required; set JEV_PYTHON to its executable path." >&2; exit 1; }
mkdir -p "$LOGDIR"
touch "$LOGDIR/jev-router.out.log" "$LOGDIR/jev-router.err.log"
chmod 600 "$LOGDIR/jev-router.out.log" "$LOGDIR/jev-router.err.log"
mkdir -p "$(dirname "$PLIST")"
mkdir -p "$RUNTIME/server" "$RUNTIME/router"
chmod 700 "$RUNTIME"
rsync -a --exclude .env --exclude '.env.*' --exclude '*.key' \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$REPO/server/" "$RUNTIME/server/"
rsync -a --exclude .venv --exclude .git --exclude test --exclude docs \
  --exclude .env --exclude '.env.*' --exclude '*.key' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude docs-site --exclude apps "$REPO/router/" "$RUNTIME/router/"
"$PYTHON" -m py_compile "$RUNTIME/server/jev_server.py"

umask 077
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$RUNTIME/server/jev_server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>$LOGDIR/jev-router.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/jev-router.err.log</string>
  <key>WorkingDirectory</key><string>$RUNTIME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CODEX_HOME</key><string>$CODEX_HOME_XML</string>
    <key>CODEX_ROUTER_STATE_DIR</key><string>$STATE_XML</string>
$DECISION_ENV_XML
$(if [ -n "$JEV_API_KEY_FILE_XML" ]; then
    printf '    <key>JEV_API_KEY_FILE</key><string>%s</string>\n' "$JEV_API_KEY_FILE_XML"
  fi)
$(if [ -n "$JEV_ENV_XML" ]; then
    printf '    <key>JEV_ENV_FILE</key><string>%s</string>\n' "$JEV_ENV_XML"
  fi)
  </dict>
</dict>
</plist>
EOF
chmod 600 "$PLIST"

# Replace the managed launchd jobs without stopping unrelated Python processes.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/io.0xnatoshi.jev-router.plist"
launchctl bootout "gui/$(id -u)/io.0xnatoshi.jev-router" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1.5
if "$PYTHON" "$REPO/server/healthcheck.py"; then
  echo ""
  echo "— Jev Router service OK ($LABEL)"
else
  echo "Jev Router health check failed" >&2
  exit 1
fi
echo "Uninstall: launchctl bootout gui/\$(id -u)/$LABEL"
