#!/bin/bash
# poker_uninstall.sh — removes all accounts and game files
NUM_PLAYERS=${1:-16}
userdel -r dealer 2>/dev/null || true
groupdel dealer   2>/dev/null || true
for i in $(seq 0 $((NUM_PLAYERS - 1))); do
    userdel -r "player$i" 2>/dev/null || true
    groupdel  "player$i"  2>/dev/null || true
done
rm -rf /home/dealer 2>/dev/null || true
echo "Uninstall complete."
