"""
poker_client.py
===============
Player client for the multiplayer poker game.
Run as a playerN account (player0 … player15).

Usage:
    python3 poker_client.py

The script derives the player number automatically from the current
Unix username (e.g. player3  →  id=3).

Commands during a game:
    FOLD            fold your hand
    CALL            call the current bet
    RAISE:<n>       raise by n chips  (e.g.  RAISE:50)
    CHAT:<text>     send a message to all players
                    ← this is the command injection vector when the
                      dealer has readVulnerability_2=1

The receiver thread prints dealer messages while you type, matching
the Virtual Werewolves client architecture.
"""

import sys, os, pwd, threading, time

# ── Determine player identity from Unix username ──────────────────────────────
try:
    username = pwd.getpwuid(os.getuid())[0]          # e.g. "player3"
    player_id = int(username.replace('player', ''))  # →  3
except (KeyError, ValueError):
    print('[ERROR] Must be run as a playerN account (e.g. player0, player3).')
    sys.exit(1)

pipeRoot  = '/home/dealer/pipes/'
OUT_PIPE  = '%s%stodD/%stod'   % (pipeRoot, player_id, player_id)   # → dealer
IN_PIPE   = '%sdto%sD/dto%s'   % (pipeRoot, player_id, player_id)   # ← dealer


# ── I/O helpers ───────────────────────────────────────────────────────────────
def send_msg(msg):
    """Write a message to the player→dealer pipe."""
    try:
        with open(OUT_PIPE, 'w') as f:
            f.write(':%d:%s\n' % (player_id, msg))
    except OSError as e:
        print('\n[client] Send error: %s' % e)


def recv_loop():
    """
    Receiver thread: opens the dealer→player pipe, reads messages,
    and prints them.  Re-opens the pipe after each message so we can
    accept the next one (named-pipe semantics on Linux).
    """
    while True:
        try:
            with open(IN_PIPE, 'r') as f:
                raw = f.read().strip()
            if raw:
                # Wire format: :<sender>:<message>
                parts = raw.split(':', 2)
                msg = parts[2] if len(parts) >= 3 else raw
                _pretty_print(msg)
        except OSError:
            time.sleep(0.2)


def _pretty_print(msg):
    """Colour-code dealer messages for readability."""
    prefix = ''
    if msg.startswith('HOLE:'):
        cards = msg[5:].split('|')
        print('\n╔══════════════════════╗')
        print('║  YOUR HOLE CARDS     ║')
        for c in cards:
            print('║  %-20s║' % c)
        print('╚══════════════════════╝')
    elif msg.startswith('WINNER:'):
        print('\n★  %s' % msg[7:])
    elif msg.startswith('COMMUNITY:'):
        cards = msg[10:].split(',')
        print('\n[Community] %s' % '  '.join(cards))
    elif msg.startswith('PHASE:'):
        print('\n── %s ──' % msg[6:].upper())
    elif msg.startswith('BALANCE:'):
        print('[Balance] %s chips' % msg[8:])
    elif msg.startswith('POT:'):
        print('[Pot] %s chips' % msg[4:])
    elif msg.startswith('YOUR_TURN:'):
        details = msg[10:]
        print('\n>>> Your turn — %s' % details)
        print('>>> FOLD / CALL / RAISE:<n> / CHAT:<text>')
    elif msg.startswith('SHOWDOWN:'):
        print('[Showdown] %s' % msg[9:])
    elif msg.startswith('INFO:'):
        print('[Info] %s' % msg[5:])
    elif msg.startswith('CHAT:'):
        print('[Chat] %s' % msg[5:])
    elif msg.startswith('ERROR:'):
        print('[!] %s' % msg[6:])
    elif msg.startswith('GAME_START:') or msg.startswith('GAME_OVER:'):
        print('\n*** %s ***' % msg.split(':', 1)[1])
    elif msg.startswith('NEW_HAND:'):
        print('\n' + '─' * 40)
        print('  New hand %s' % msg[9:])
        print('─' * 40)
    else:
        print('[Dealer] %s' % msg)
    print('> ', end='', flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print('╔══════════════════════════════════════╗')
    print('║       PYTHON POKER  –  Player %-2d    ║' % player_id)
    print('║  FOLD / CALL / RAISE:<n> / CHAT:<t>  ║')
    print('╚══════════════════════════════════════╝')
    print('[client] Connecting as player %d...' % player_id)

    # Start background receiver thread
    t = threading.Thread(target=recv_loop, daemon=True)
    t.start()

    # Send READY to trigger the dealer's wait_for_players() unblock
    send_msg('READY')
    print('[client] Sent READY — waiting for game to start...\n')

    # Input loop
    while True:
        try:
            line = input('> ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\n[client] Disconnected.')
            break
        if not line:
            continue
        send_msg(line)


if __name__ == '__main__':
    main()
