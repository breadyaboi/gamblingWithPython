#!/bin/bash
# poker_install.sh  (Project #2)
# =============================
# Creates the dealer account, playerN accounts and the game directories.
#
# Usage:  sudo bash poker_install.sh [num_players]      (default 16)
#
# Layout and permissions:
#   /opt/poker/          root:pokerplayers  0750   shared library + client (read-only)
#   /opt/poker-dealer/   root:root          0700   dealer.py
#   /var/lib/poker/      dealer:pokerplayers 2750  holds game.sock (0660)
#   /home/dealer/log/    dealer:dealer      0700   chat.log, dealer.log (0600)
#   /sys/fs/cgroup/poker/                          created by the dealer at start
#
# The dealer is started as root (to create the engine's cgroup), so its code
# lives in a root-owned directory: if it sat in /home/dealer, the dealer
# account could edit code that root later runs.
#
# Players are in the `pokerplayers` group only, NOT the dealer group (Project #1
# put them in the dealer group and made the socket 0777).

set -euo pipefail
cd "$(dirname "$0")"

NUM_PLAYERS=${1:-16}
if ! [[ "$NUM_PLAYERS" =~ ^[0-9]+$ ]] || (( NUM_PLAYERS < 2 || NUM_PLAYERS > 16 )); then
    echo "num_players must be 2-16"; exit 1
fi
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

echo "[install] Groups and dealer account..."
getent group pokerplayers >/dev/null || groupadd pokerplayers
id dealer &>/dev/null || adduser --disabled-password --gecos "" dealer >/dev/null
usermod -aG pokerplayers dealer

echo "[install] Shared library -> /opt/poker"
install -d -o root -g pokerplayers -m 0750 /opt/poker
for f in cards.py banking.py poker_communication.py poker_client.py attack_client.py; do
    install -o root -g pokerplayers -m 0644 "$f" /opt/poker/
done
rm -rf /opt/poker/__pycache__

echo "[install] Dealer files -> /opt/poker-dealer"
install -d -o root -g root -m 0700 /opt/poker-dealer
install -o root -g root -m 0700 dealer.py  /opt/poker-dealer/
rm -rf /opt/poker-dealer/__pycache__ /opt/poker-dealer/sandbox.py   # now part of poker_communication.py
chown dealer:dealer /home/dealer
chmod 0700 /home/dealer
install -d -o dealer -g dealer -m 0700 /home/dealer/log
# Remove leftovers from earlier layouts
rm -rf /home/dealer/sockets /home/dealer/poker_client.py /home/dealer/cards.py \
       /home/dealer/banking.py /home/dealer/poker_communication.py \
       /home/dealer/dealer.py /home/dealer/sandbox.py

echo "[install] Socket directory -> /var/lib/poker"
install -d -o dealer -g pokerplayers -m 2750 /var/lib/poker

echo "[install] Player accounts..."
for i in $(seq 0 $((NUM_PLAYERS - 1))); do
    U="player$i"
    id "$U" &>/dev/null || adduser --disabled-password --gecos "" "$U" >/dev/null
    usermod -aG pokerplayers "$U"
    gpasswd -d "$U" dealer &>/dev/null || true     # undo Project #1 membership
    rm -f "/home/$U/cards.py" "/home/$U/banking.py" "/home/$U/poker_client.py"
    ln -sf /opt/poker/poker_client.py "/home/$U/poker_client.py"
    ln -sf /opt/poker/attack_client.py "/home/$U/attack_client.py"
done

echo ""
echo "[install] Done. $NUM_PLAYERS players created."
echo ""
echo "  Start dealer:   sudo python3 /opt/poker-dealer/dealer.py <num_players>"
echo "  Player login:   sudo su - player0"
echo "  Player client:  python3 ~/poker_client.py"
