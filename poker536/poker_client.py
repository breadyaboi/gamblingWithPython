"""
poker_client.py
===============
Player client for the Python Poker game.  Run it as your playerN account:

    python3 /opt/poker/poker_client.py

Commands (case-insensitive):
    FOLD   CHECK   CALL   RAISE:<n>   ALLIN
    CHAT:<text>          message everyone        (shortcut:  say <text>)
    MSG:<id>:<text>      private message to one  (shortcut:  @<id> <text>)
    WHO                  balances and who's connected
    HELP
"""

import sys, os, socket, pwd, threading
sys.path.insert(0, '/opt/poker')

from poker_communication import (
    SOCKET_PATH, MAX_LINE, frame, unframe, clean_text,
    render_cards, parse_card, parse_cards,
)

MAX_INBUF = 64 * 1024

# ── Identity ─────────────────────────────────────────────────────────────────
def _get_player_id():
    name = pwd.getpwuid(os.getuid())[0]
    if name.startswith('player') and name[6:].isdigit():
        return int(name[6:])
    return None

PLAYER_ID = _get_player_id()
if PLAYER_ID is None:
    print('[client] ERROR: this client must be run as a playerN account.')
    sys.exit(1)

# ── Socket ───────────────────────────────────────────────────────────────────
_sock = None
_print_lock = threading.Lock()

def connect():
    global _sock
    _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _sock.connect(SOCKET_PATH)

def send_msg(body):
    data = frame(str(PLAYER_ID), body)
    if len(data) > MAX_LINE:
        out('%s[!] Message too long (max about %d characters).%s' % (RED, MAX_LINE - 20, RESET))
        return
    try:
        _sock.sendall(data)
    except OSError as e:
        out('[!] Send error: %s' % e)

# ── Display ──────────────────────────────────────────────────────────────────
RESET, BOLD = '\033[0m', '\033[1m'
GREEN, YELLOW, CYAN, RED, MAGENTA = '\033[32m', '\033[33m', '\033[36m', '\033[31m', '\033[35m'

def out(*lines):
    with _print_lock:
        for l in lines:
            print(l)
        print('> ', end='', flush=True)

def _box(text, title):
    w = max(len(text), len(title)) + 4
    return ['╔' + '═' * (w - 2) + '╗',
            '║ ' + BOLD + title + RESET + ' ' * (w - 3 - len(title)) + '║',
            '╠' + '═' * (w - 2) + '╣',
            '║ ' + text + ' ' * (w - 3 - len(text)) + '║',
            '╚' + '═' * (w - 2) + '╝']

def _cards(cards, title):
    return ['', BOLD + title + RESET] + ['  ' + r for r in render_cards(cards)] + ['']

def _pretty(msg):
    tag, _, rest = msg.partition(':')
    tag = tag.upper()
    rest = clean_text(rest, 1000)        # never let escape codes reach the terminal

    if tag == 'HOLE':
        c = parse_cards(rest)
        return _cards(c, '  YOUR HOLE CARDS  ') if c else ['[Hole] ' + rest]
    if tag == 'COMMUNITY':
        c = parse_cards(rest)
        return _cards(c, '  BOARD  ') if c else ['[Board] ' + rest]
    if tag == 'SHOWDOWN':
        lines = ['%s[Showdown]%s %s' % (YELLOW, RESET, rest)]
        if ' shows ' in rest:
            c = [x for x in (parse_card(t) for t in rest.partition(' shows ')[2].split(',')) if x]
            lines += ['  ' + r for r in render_cards(c)]
        return lines
    if tag == 'WINNER':
        return ['%s★  %s%s' % (YELLOW, rest, RESET)]
    if tag == 'PHASE':
        return ['─' * 50, '%s  ── %s ──%s' % (BOLD, rest.upper(), RESET), '─' * 50]
    if tag == 'BALANCE':
        return ['%s[Balance]%s  $%s chips' % (GREEN, RESET, rest)]
    if tag == 'POT':
        return ['[Pot]     $%s chips' % rest]
    if tag == 'YOUR_TURN':
        kv = dict(p.split('=', 1) for p in rest.split() if '=' in p)
        owed = kv.get('to_call', '?')
        opts = 'CHECK' if owed == '0' else 'CALL'
        return ['', '%s>>> Your turn  │  To call: $%s  │  Balance: $%s  │  Pot: $%s  │  %ss%s' % (
                    BOLD, owed, kv.get('balance', '?'), kv.get('pot', '?'),
                    kv.get('timeout', '?'), RESET),
                '>>> FOLD / %s / RAISE:<n> (min %s) / ALLIN' % (opts, kv.get('min_raise', '?'))]
    if tag == 'TURN':
        return ['%s%s%s' % ('\033[2m', rest, RESET)]
    if tag == 'ACK':
        return ['%s[✓]%s %s' % (GREEN, RESET, rest)]
    if tag in ('INFO', 'WELCOME'):
        return ['[Info] ' + rest]
    if tag == 'STATUS':
        return ['[Table] ' + p for p in rest.split('  ') if p]
    if tag == 'CHAT':
        return ['%s[Chat]%s %s' % (CYAN, RESET, rest)]
    if tag == 'DM':
        return ['%s[Private]%s %s' % (MAGENTA, RESET, rest)]
    if tag == 'ERROR':
        return ['%s[!] %s%s' % (RED, rest, RESET)]
    if tag in ('GAME_START', 'GAME_OVER'):
        return [''] + _box(rest, tag.replace('_', ' '))
    if tag == 'NEW_HAND':
        return ['', '═' * 50, '%s  New hand %s%s' % (BOLD, rest, RESET), '═' * 50]
    if tag == 'IDENTIFY':
        return []
    return ['[Dealer] ' + clean_text(msg, 1000)]

# ── Receiver thread (concurrent with the input loop) ─────────────────────────
def recv_loop():
    buf = b''
    while True:
        try:
            chunk = _sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            sender, body = unframe(line)
            if sender != 'd' or not body:
                continue                 # only the dealer speaks on this socket
            lines = _pretty(body)
            if lines:
                out(*lines)
        if len(buf) > MAX_INBUF:
            buf = b''
    out('', '[client] Disconnected from dealer. Press Enter to exit.')
    os._exit(0)

# ── Input translation ────────────────────────────────────────────────────────
GAME_WORDS = {'FOLD', 'CHECK', 'CALL', 'ALLIN', 'ALL-IN', 'WHO', 'STATUS', 'HELP'}

def translate(line):
    """Turn what the user typed into a protocol message, or None."""
    up = line.upper()
    if up.startswith('CHAT:'):
        return 'CHAT:' + line[5:]
    if up.startswith('SAY '):
        return 'CHAT:' + line[4:]
    if up.startswith('MSG:'):
        return 'MSG:' + line[4:]
    if line.startswith('@'):
        target, _, text = line[1:].partition(' ')
        if target.isdigit() and text:
            return 'MSG:%s:%s' % (target, text)
        out('[!] Use  @<player_id> <message>')
        return None
    if up in GAME_WORDS:
        return 'ALLIN' if up == 'ALL-IN' else up
    if up.startswith('RAISE') or up.startswith('BET'):
        return up.replace(' ', ':', 1).replace('::', ':')
    out('%s[!] Unknown command. FOLD CHECK CALL RAISE:<n> ALLIN  say <text>  @<id> <text>  WHO%s'
        % (RED, RESET))
    return None

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print('╔══════════════════════════════════════╗')
    print('║       PYTHON POKER  –  Player %-2d     ║' % PLAYER_ID)
    print('║  FOLD CHECK CALL RAISE:<n> ALLIN     ║')
    print('║  say <text>   @<id> <text>   WHO     ║')
    print('╚══════════════════════════════════════╝')
    try:
        connect()
    except OSError as e:
        print('[client] Cannot connect to %s: %s' % (SOCKET_PATH, e))
        print('         Is the dealer running, and are you in the pokerplayers group?')
        sys.exit(1)
    print('[client] Connected. Waiting for the game to start...\n')

    threading.Thread(target=recv_loop, daemon=True).start()
    send_msg('READY:%d' % PLAYER_ID)

    while True:
        try:
            line = input('> ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\n[client] Leaving the table.')
            break
        if line:
            msg = translate(line)
            if msg:
                send_msg(msg)
    _sock.close()


if __name__ == '__main__':
    main()
