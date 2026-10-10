#!/bin/bash
# poker_install.sh  (Unix domain socket edition)
# ===============================================
# Set up the dealer account, player accounts, and game files.
# No named pipes — communication uses a Unix domain socket.
#
# Usage:  sudo bash poker_install.sh [num_players]
# Default num_players: 16

set -e

NUM_PLAYERS=${1:-16}

echo "[install] Creating dealer account..."
adduser --disabled-password --gecos "" dealer 2>/dev/null || true

echo "[install] Creating directories..."
mkdir -p /home/dealer/sockets
mkdir -p /home/dealer/log

echo "[install] Copying game files..."
cp cards.py               /home/dealer/
cp banking.py             /home/dealer/
cp poker_communication.py /home/dealer/
cp dealer.py              /home/dealer/
cp poker_client.py        /home/dealer/

chown -R dealer:dealer /home/dealer/
chmod  750             /home/dealer/
chmod  750             /home/dealer/sockets/
chmod  700             /home/dealer/log/

echo "[install] Creating player accounts..."
for i in $(seq 0 $((NUM_PLAYERS - 1))); do
    USERNAME="player$i"
    adduser --disabled-password --gecos "" "$USERNAME" 2>/dev/null || true
    usermod -aG dealer "$USERNAME"   # add to dealer group so they can reach the socket

    # Give each player their own copy of the client
    cp /home/dealer/poker_client.py "/home/$USERNAME/"
    cp /home/dealer/cards.py        "/home/$USERNAME/"
    cp /home/dealer/banking.py      "/home/$USERNAME/"
    chown "$USERNAME":"$USERNAME"   "/home/$USERNAME/poker_client.py"
    chown "$USERNAME":"$USERNAME"   "/home/$USERNAME/cards.py"
    chown "$USERNAME":"$USERNAME"   "/home/$USERNAME/banking.py"
    chmod 750 "/home/$USERNAME/poker_client.py"
    echo "[install] Created $USERNAME"
done

echo ""
echo "[install] Done.  $NUM_PLAYERS players created."
echo ""
echo "  Start dealer:   sudo -u dealer python3 /home/dealer/dealer.py $NUM_PLAYERS"
echo "  Player login:   su - player0"
echo "  Player client:  python3 ~/poker_client.py"
echo ""
echo "  To demonstrate impersonation attack:"
echo "    python3 /home/dealer/poker_client.py --as 0"
echo "    (connect before player 0 to receive their hole cards)"
