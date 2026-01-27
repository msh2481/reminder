#!/usr/bin/env bash
set -euo pipefail

LABEL="com.mike.reminder"
DOMAIN="gui/$(id -u)"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

# Restart LaunchAgent (assumes plist already installed at $PLIST)
launchctl bootout "${DOMAIN}" "${PLIST}" >/dev/null 2>&1 || true
launchctl bootstrap "${DOMAIN}" "${PLIST}" 2>/dev/null || launchctl bootstrap "${DOMAIN}" "${PLIST}"
launchctl enable "${DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
launchctl kickstart -k "${DOMAIN}/${LABEL}"

