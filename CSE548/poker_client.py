"""
poker_client.py
===============
Player client for the Python Poker game.
Run as your assigned playerN account.

Commands during play:
    FOLD             fold your hand
    CALL             call the current bet
    RAISE:<n>        raise by n chips  (e.g.  RAISE:50)
    CHAT:<text>      send a chat message to all players
"""

import sys, os, socket, pwd, threading
sys.path.insert(0, '/home/dealer')

from poker_communication import (
    SOCKET_PATH, frame,
    render_cards, parse_card, parse_cards,
)

# ── Identity ──────────────────────────────────────────────────────────────────
def _get_player_id():
    username = pwd.getpwuid(os.getuid())[0]
    try:
        return int(username.replace('player', ''))
    except ValueError:
        return None

PLAYER_ID = _get_player_id()
if PLAYER_ID is None:
    print('[client] ERROR: this client must be run as a playerN account.')
    sys.exit(1)

# ── Socket ────────────────────────────────────────────────────────────────────
_sock     = None
_recv_buf = b''

def connect():
    global _sock
    _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _sock.connect(SOCKET_PATH)
    _sock.setblocking(True)

def send_msg(body):
    try:
        _sock.sendall(frame(str(PLAYER_ID), body))
    except OSError as e:
        print('\n[!] Send error: %s' % e)

# ── Display helpers ───────────────────────────────────────────────────────────
RESET  = '\033[0m'
BOLD   = '\033[1m'
GREEN  = '\033[32m'
YELLOW = '\033[33m'
CYAN   = '\033[36m'
RED    = '\033[31m'

def _box(lines, title=''):
    w = max((len(l) for l in lines), default=20) + 4
    w = max(w, len(title) + 4)
    print('╔' + '═' * (w-2) + '╗')
    if title:
        pad = w - 2 - len(title)
        print('║ ' + BOLD + title + RESET + ' ' * (pad-1) + '║')
        print('╠' + '═' * (w-2) + '╣')
    for l in lines:
        pad = w - 2 - len(l)
        print('║ ' + l + ' ' * (pad-1) + '║')
    print('╚' + '═' * (w-2) + '╝')

def _show_cards(cards, title):
    print()
    print(BOLD + title + RESET)
    for row in render_cards(cards):
        print('  ' + row)
    print()

def _pretty(msg):
    tag, _, rest = msg.partition(':')
    tag = tag.upper()

    if tag == 'HOLE':
        cards = parse_cards(rest, sep='|')
        if cards:
            _show_cards(cards, '  YOUR HOLE CARDS  ')
        else:
            print('\n[Hole] %s' % rest)

    elif tag == 'COMMUNITY':
        cards = parse_cards(rest, sep='|')
        if cards:
            _show_cards(cards, '  BOARD  ')
        else:
            print('\n[Board] %s' % rest)

    elif tag == 'SHOWDOWN':
        print('\n%s[Showdown]%s %s' % (YELLOW, RESET, rest))
        if ' shows ' in rest:
            _, _, card_part = rest.partition(' shows ')
            cards = [parse_card(t) for t in card_part.split(',')]
            cards = [c for c in cards if c]
            if cards:
                for row in render_cards(cards):
                    print('  ' + row)

    elif tag == 'WINNER':
        print('\n%s★  %s%s' % (YELLOW, rest, RESET))

    elif tag == 'PHASE':
        print('\n' + '─' * 50)
        print('%s  ── %s ──%s' % (BOLD, rest.upper(), RESET))
        print('─' * 50)

    elif tag == 'BALANCE':
        print('%s[Balance]%s  $%s chips' % (GREEN, RESET, rest))

    elif tag == 'POT':
        print('[Pot]     $%s chips' % rest)

    elif tag == 'YOUR_TURN':
        parts = dict(p.split('=') for p in rest.split() if '=' in p)
        to_call = parts.get('to_call', '?')
        balance = parts.get('balance', '?')
        print()
        print('%s>>> Your turn  │  To call: $%s  │  Balance: $%s%s' % (
            BOLD, to_call, balance, RESET))
        print('>>> FOLD  /  CALL  /  RAISE:<n>  /  CHAT:<text>')

    elif tag == 'ACK':
        print('%s[✓]%s %s' % (GREEN, RESET, rest))

    elif tag == 'INFO':
        print('[Info] %s' % rest)

    elif tag == 'CHAT':
        print('%s[Chat]%s %s' % (CYAN, RESET, rest))

    elif tag == 'ERROR':
        print('%s[!] %s%s' % (RED, rest, RESET))

    elif tag == 'GAME_START':
        print()
        _box([rest], title='GAME START')

    elif tag == 'GAME_OVER':
        print()
        _box([rest], title='GAME OVER')

    elif tag == 'NEW_HAND':
        print('\n' + '═' * 50)
        print('%s  New hand %s%s' % (BOLD, rest, RESET))
        print('═' * 50)

    elif tag == 'IDENTIFY':
        pass

    else:
        print('[Dealer] %s' % msg)

    print('> ', end='', flush=True)


# ── Receiver thread ───────────────────────────────────────────────────────────
def recv_loop():
    global _recv_buf
    while True:
        try:
            chunk = _sock.recv(4096)
        except OSError:
            break
        if not chunk:
            print('\n[client] Disconnected from dealer.')
            break
        _recv_buf += chunk
        while b'\n' in _recv_buf:
            line, _recv_buf = _recv_buf.split(b'\n', 1)
            text = line.decode(errors='replace').strip()
            if not text:
                continue
            parts = text.split(':', 2)
            body  = parts[2] if len(parts) >= 3 else text
            _pretty(body)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print('╔══════════════════════════════════════╗')
    print('║       PYTHON POKER  –  Player %-2d    ║' % PLAYER_ID)
    print('║  FOLD / CALL / RAISE:<n> / CHAT:<t>  ║')
    print('╚══════════════════════════════════════╝')

    print('[client] Connecting...')
    try:
        connect()
    except OSError as e:
        print('[client] Cannot connect: %s' % e)
        sys.exit(1)
    print('[client] Connected.')

    t = threading.Thread(target=recv_loop, daemon=True)
    t.start()

    send_msg('READY:%d' % PLAYER_ID)
    print('[client] Waiting for game to start...\n')

    while True:
        try:
            line = input('> ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\n[client] Disconnected.')
            break
        if not line:
            continue
        send_msg(line)

    _sock.close()


if __name__ == '__main__':
    main()
