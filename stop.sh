#!/usr/bin/env bash
set -euo pipefail

LABEL="com.mike.reminder"
DOMAIN="gui/$(id -u)"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

# Stop/unload LaunchAgent (ignore errors if not loaded)
launchctl bootout "${DOMAIN}" "${PLIST}" >/dev/null 2>&1 || true
