#!/bin/sh
set -eu

PLIST_PATH="$HOME/Library/LaunchAgents/com.microduck.trade-notifier.plist"
launchctl bootout "gui/$(id -u)/com.microduck.trade-notifier" 2>/dev/null || true
if [ -f "$PLIST_PATH" ]; then
  mv "$PLIST_PATH" "$HOME/.Trash/com.microduck.trade-notifier.plist"
fi
echo "Microduck 后台交易通知已停止，配置已移到废纸篓。"
