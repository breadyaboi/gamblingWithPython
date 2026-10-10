#!/bin/bash
# poker_install.sh
# ================
# Set up the dealer account, directories, and all player accounts.
# Run from the directory containing the poker game files.
#
# Requires: sudo access
# Mirrors the structure of Virtual Werewolves install.sh

set -e

echo "[install] Creating dealer account..."
sudo adduser --disabled-password --gecos "" dealer 2>/dev/null || true

echo "[install] Creating directory structure..."
sudo mkdir -p /home/dealer/pipes
sudo mkdir -p /home/dealer/log

echo "[install] Copying game files..."
sudo cp cards.py             /home/dealer/
sudo cp banking.py           /home/dealer/
sudo cp poker_communication.py /home/dealer/
sudo cp dealer.py            /home/dealer/
sudo cp poker_client.py      /home/dealer/
sudo cp makepoker_users.py   /home/dealer/

sudo chmod a+x /home/dealer

# Create an empty log placeholder; makepoker_users.py sets final permissions
sudo touch /home/dealer/log/dealer.log
sudo touch /home/dealer/log/dummy.log

# Set ownership of dealer home
sudo chown -R dealer:dealer /home/dealer/
sudo chmod 750 /home/dealer/
sudo chmod 700 /home/dealer/log/
sudo chmod 600 /home/dealer/log/dealer.log

echo "[install] Creating player accounts and named pipes..."
# Pass number of players as first argument, default 16
NUM_PLAYERS=${1:-16}
sudo python3 makepoker_users.py "$NUM_PLAYERS"

echo ""
echo "[install] Done."
echo ""
echo "  Start the dealer:   sudo -u dealer python3 /home/dealer/dealer.py $NUM_PLAYERS"
echo "  Player login:       ssh player0@localhost"
echo "  Player client:      python3 ~/poker_client.py"
