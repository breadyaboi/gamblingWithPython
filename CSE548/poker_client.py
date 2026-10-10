"""
poker_client.py  (Unix domain socket edition)
==============================================
Player client.  Run as a playerN account.

Usage:
    python3 poker_client.py

The player ID is derived from the Unix username automatically.
A background thread reads from the socket and prints dealer messages;
the main thread reads stdin and sends player actions.

Commands:
    FOLD             fold your hand
    CALL             call the current bet
    RAISE:<n>        raise by n chips
    CHAT:<text>      send a message  ← command injection vector
                     when dealer has readVulnerability_2=1

Attack notes
------------
Impersonation (readVulnerability=1 on dealer):
    The dealer trusts the READY:<id> message without verifying
    who you actually are.  To impersonate player 0:

        python3 poker_client.py --as 0

    Connect before player 0 does.  You will receive player 0's
    hole cards.  This is a Unix socket equivalent of spoofing the
    pipe-open wait state in Virtual Werewolves.

Command injection (readVulnerability_2=1 on dealer):
    The dealer passes CHAT text to os.popen().  Send:

        CHAT:hi"; cat /home/dealer/log/dealer.log > /tmp/stolen;"

    and the file is copied to /tmp/stolen where you can read it.
"""

import sys, os, socket, pwd, threading, time
sys.path.insert(0, '/home/dealer')

from poker_communication import SOCKET_PATH, frame

# ── Identity ──────────────────────────────────────────────────────────────────
def _get_player_id():
    username = pwd.getpwuid(os.getuid())[0]   # e.g. "player3"
    try:
        return int(username.replace('player', ''))
    except ValueError:
        return None

# Allow --as <N> to impersonate a different player (demonstrates the vuln)
if '--as' in sys.argv:
    idx = sys.argv.index('--as')
    try:
        PLAYER_ID = int(sys.argv[idx + 1])
        print('[client] *** IMPERSONATION MODE — claiming to be player %d ***'
              % PLAYER_ID)
    except (IndexError, ValueError):
        print('[client] --as requires a player number')
        sys.exit(1)
else:
    PLAYER_ID = _get_player_id()
    if PLAYER_ID is None:
        print('[client] ERROR: must run as a playerN account '
              'or pass --as <N>')
        sys.exit(1)


# ── Socket I/O ────────────────────────────────────────────────────────────────
_sock = None

def connect():
    global _sock
    _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _sock.connect(SOCKET_PATH)
    _sock.setblocking(True)

def send_msg(body):
    try:
        _sock.sendall(frame(str(PLAYER_ID), body))
    except OSError as e:
        print('\n[client] Send error: %s' % e)


# ── Pretty printer ────────────────────────────────────────────────────────────
def _pretty(msg):
    tag, _, rest = msg.partition(':')
    tag = tag.upper()

    if tag == 'HOLE':
        cards = rest.split('|')
        print('\n╔══════════════════════╗')
        print('║   YOUR HOLE CARDS    ║')
        for c in cards:
            print('║  %-20s║' % c)
        print('╚══════════════════════╝')
    elif tag == 'WINNER':
        print('\n★  %s' % rest)
    elif tag == 'COMMUNITY':
        print('\n[Board]  %s' % '  '.join(rest.split(',')))
    elif tag == 'PHASE':
        print('\n── %s ──' % rest.upper())
    elif tag == 'BALANCE':
        print('[Balance] %s chips' % rest)
    elif tag == 'POT':
        print('[Pot]     %s chips' % rest)
    elif tag == 'YOUR_TURN':
        print('\n>>> Your action  (%s)' % rest)
        print('>>> FOLD / CALL / RAISE:<n> / CHAT:<text>')
    elif tag == 'SHOWDOWN':
        print('[Showdown] %s' % rest)
    elif tag in ('INFO', 'ACK'):
        print('[%s] %s' % (tag.title(), rest))
    elif tag == 'CHAT':
        print('[Chat] %s' % rest)
    elif tag == 'ERROR':
        print('[!] %s' % rest)
    elif tag in ('GAME_START', 'GAME_OVER', 'NEW_HAND'):
        print('\n*** %s ***' % rest)
    elif tag == 'IDENTIFY':
        pass   # handled in connect flow; suppress from display
    else:
        print('[Dealer] %s' % msg)
    print('> ', end='', flush=True)


# ── Receiver thread ───────────────────────────────────────────────────────────
_recv_buf = b''

def recv_loop():
    global _recv_buf
    while True:
        try:
            chunk = _sock.recv(4096)
        except OSError:
            break
        if not chunk:
            print('\n[client] Connection closed by dealer.')
            break
        _recv_buf += chunk
        while b'\n' in _recv_buf:
            line, _recv_buf = _recv_buf.split(b'\n', 1)
            text = line.decode(errors='replace').strip()
            if not text:
                continue
            # Wire format: :<sender>:<body>
            parts = text.split(':', 2)
            body = parts[2] if len(parts) >= 3 else text
            _pretty(body)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print('╔══════════════════════════════════════╗')
    print('║       PYTHON POKER  –  Player %-2d    ║' % PLAYER_ID)
    print('║  FOLD / CALL / RAISE:<n> / CHAT:<t>  ║')
    print('╚══════════════════════════════════════╝')
    print('[client] Connecting to %s ...' % SOCKET_PATH)

    try:
        connect()
    except OSError as e:
        print('[client] Cannot connect: %s' % e)
        sys.exit(1)

    print('[client] Connected.')

    # Start background receiver
    t = threading.Thread(target=recv_loop, daemon=True)
    t.start()

    # Identify to dealer
    send_msg('READY:%d' % PLAYER_ID)
    print('[client] Sent READY. Waiting for game to start...\n')

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

    _sock.close()


if __name__ == '__main__':
    main()
