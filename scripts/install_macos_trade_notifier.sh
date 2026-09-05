#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
PYTHON_BIN=$(command -v python3)
DOCKER_BIN=$(command -v docker)
AGENT_DIR="$HOME/Library/LaunchAgents"
DATA_DIR="$HOME/Library/Application Support/Microduck"
LOG_DIR="$HOME/Library/Logs/Microduck"
PLIST_PATH="$AGENT_DIR/com.microduck.trade-notifier.plist"
STATE_PATH="$DATA_DIR/trade-notifier-state.json"

mkdir -p "$AGENT_DIR" "$DATA_DIR" "$LOG_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.microduck.trade-notifier</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$SCRIPT_DIR/macos_trade_notifier.py</string>
    <string>--env-file</string>
    <string>$PROJECT_DIR/.env</string>
    <string>--state-file</string>
    <string>$STATE_PATH</string>
    <string>--interval</string>
    <string>5</string>
    <string>--docker-bin</string>
    <string>$DOCKER_BIN</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/trade-notifier.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/trade-notifier-error.log</string>
</dict>
</plist>
EOF

chmod 600 "$PLIST_PATH"
launchctl bootout "gui/$(id -u)/com.microduck.trade-notifier" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/com.microduck.trade-notifier"

"$PYTHON_BIN" "$SCRIPT_DIR/macos_trade_notifier.py" \
  --env-file "$PROJECT_DIR/.env" \
  --state-file "$STATE_PATH" \
  --test-notification

echo "Microduck 后台交易通知已安装并启动。"
