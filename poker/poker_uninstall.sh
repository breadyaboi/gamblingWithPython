#!/bin/bash
# poker_uninstall.sh — removes all accounts, groups and game files.
# Usage:  sudo bash poker_uninstall.sh [num_players]      (default 16)
NUM_PLAYERS=${1:-16}
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
pkill -u dealer 2>/dev/null || true
for i in $(seq 0 $((NUM_PLAYERS - 1))); do
    pkill -u "player$i" 2>/dev/null || true
    userdel -r "player$i" 2>/dev/null || true
    groupdel  "player$i"  2>/dev/null || true
done
userdel -r dealer 2>/dev/null || true
groupdel dealer       2>/dev/null || true
groupdel pokerplayers 2>/dev/null || true
rm -rf /home/dealer /opt/poker /var/lib/poker
echo "Uninstall complete."
