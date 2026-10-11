"""
poker_communication.py  (Unix domain socket edition)
=====================================================
Core I/O + card-rendering library for the multiplayer poker game.

Communication model
-------------------
A single Unix domain socket at /home/dealer/sockets/game.sock.
All players connect here; the dealer's event loop uses select() on
[server_sock] + [all connected player sockets].

Card rendering
--------------
render_cards(cards)     → list of strings (ASCII art row of face-up cards)
render_concealed(n)     → list of strings (n face-down cards)
parse_card(text)        → Card object from "Rank of Suit" string
print_cards(cards, lbl) → print card art to stdout

NOTE: fixes the Clubs symbol bug in the original cards.py
      (translate_suit returned '♦' for Clubs; correct symbol is '♣').
"""

import os, sys, socket, select, struct, time, pwd

# ── Configuration ────────────────────────────────────────────────────────────
SOCKET_DIR  = '/home/dealer/sockets'
SOCKET_PATH = SOCKET_DIR + '/game.sock'
CHAT_LOG    = '/home/dealer/log/chat.log'
DEALER_LOG  = '/home/dealer/log/dealer.log'

_AUTH_MODE      = 1
_CHAT_LOG_MODE  = 1
_LOG_INIT_MODE  = 1
_SOCKET_MODE    = 1

# ── Card rendering ────────────────────────────────────────────────────────────
# Correct suit symbols (fixes the original cards.py Clubs bug)
_SUIT = {
    'Hearts':   '♥',
    'Diamonds': '♦',
    'Clubs':    '♣',
    'Spades':   '♠',
}

def _rank_abbr(rank):
    return '10' if rank == '10' else rank[0]

def _suit_sym(suit):
    return _SUIT.get(suit, '?')


def render_cards(cards):
    """
    Return a list of 5 strings: the ASCII art for a horizontal row of
    face-up cards.  Works for any number of cards >= 1.

    Example output (2 cards):
        ' _____   _____  '
        '|  ♥  | |  ♠  | '
        '|  A  | |  K  | '
        '|  ♥  | |  ♠  | '
        ' ‾‾‾‾‾   ‾‾‾‾‾  '
    """
    if not cards:
        return []
    rows = [
        ''.join(' _____  ' for _ in cards),
        ''.join('|  %s  | ' % _suit_sym(c.suit)  for c in cards),
        ''.join(('|  %s | ' if c.rank == '10' else '|  %s  | ')
                % _rank_abbr(c.rank) for c in cards),
        ''.join('|  %s  | ' % _suit_sym(c.suit)  for c in cards),
        ''.join(' ‾‾‾‾‾  ' for _ in cards),
    ]
    return rows


def render_concealed(n):
    """Return ASCII art for *n* face-down cards."""
    if n <= 0:
        return []
    return [
        ''.join(' _____  ' for _ in range(n)),
        ''.join('| PY  | ' for _ in range(n)),
        ''.join('| TH  | ' for _ in range(n)),
        ''.join('| ON  | ' for _ in range(n)),
        ''.join(' ‾‾‾‾‾  ' for _ in range(n)),
    ]


def print_cards(cards, label=''):
    """Print graphical card art, with an optional header label."""
    if label:
        print(label)
    for line in render_cards(cards):
        print(line)
    print()


def parse_card(text):
    """
    Parse 'Rank of Suit' (Card.__str__ format) back into a Card object.
    Returns None on malformed input.
    """
    from cards import Card
    text = text.strip()
    if ' of ' in text:
        rank, suit = text.split(' of ', 1)
        return Card(suit=suit.strip(), rank=rank.strip())
    return None


def parse_cards(text, sep='|'):
    """Parse a sep-separated string of 'Rank of Suit' tokens into a Card list."""
    result = []
    for token in text.split(sep):
        c = parse_card(token)
        if c:
            result.append(c)
    return result


# ── Server socket lifecycle ───────────────────────────────────────────────────
def create_server_socket():
    os.makedirs(SOCKET_DIR, exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    srv.listen(16)
    srv.setblocking(False)
    if _SOCKET_MODE == 1:
        os.chmod(SOCKET_DIR,  0o777)
        os.chmod(SOCKET_PATH, 0o777)
    else:
        os.chmod(SOCKET_DIR,  0o750)
        os.chmod(SOCKET_PATH, 0o660)
    return srv


def accept_client(srv):
    conn, _ = srv.accept()
    conn.setblocking(False)
    return conn


# ── Authentication ────────────────────────────────────────────────────────────
def authenticate(conn, claimed_id):
    if _AUTH_MODE == 1:
        return True
    try:
        cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                               struct.calcsize('3i'))
        pid, uid, gid = struct.unpack('3i', cred)
        username = pwd.getpwuid(uid).pw_name
        return username == ('player%d' % claimed_id)
    except (OSError, KeyError):
        return False


# ── Message framing ───────────────────────────────────────────────────────────
_recv_buf = {}

def frame(sender, body):
    return (':%s:%s\n' % (sender, body)).encode()

def send_to(conn, body, sender='d'):
    try:
        conn.sendall(frame(sender, body))
    except OSError:
        pass

def recv_from(conn):
    key = conn.fileno()
    _recv_buf.setdefault(key, b'')
    try:
        chunk = conn.recv(4096)
    except (BlockingIOError, OSError):
        return []
    if not chunk:
        return []
    _recv_buf[key] += chunk
    results = []
    while b'\n' in _recv_buf[key]:
        line, _recv_buf[key] = _recv_buf[key].split(b'\n', 1)
        text = line.decode(errors='replace').strip()
        if not text:
            continue
        parts = text.split(':', 2)
        results.append((parts[1] if len(parts) >= 3 else '?',
                        parts[2] if len(parts) >= 3 else text))
    return results

def cleanup_conn(conn):
    _recv_buf.pop(conn.fileno(), None)
    try:
        conn.close()
    except OSError:
        pass


# ── Async multiplexed receive ─────────────────────────────────────────────────
def async_recv(conn_map, server_sock=None, timeout=1.0):
    watch = list(conn_map.keys())
    if server_sock is not None:
        watch.append(server_sock)
    if not watch:
        time.sleep(timeout)
        return [], []
    try:
        readable, _, _ = select.select(watch, [], watch, timeout)
    except (ValueError, OSError):
        return [], []
    messages, new_conns = [], []
    for s in readable:
        if server_sock is not None and s is server_sock:
            new_conns.append(accept_client(server_sock))
        else:
            player = conn_map.get(s)
            if player is None:
                continue
            for sender, body in recv_from(s):
                messages.append((player, body))
    return messages, new_conns


def broadcast(body, conn_map, sender='d'):
    for conn in conn_map:
        send_to(conn, body, sender)

def send_player(pid, body, conn_map, sender='d'):
    for conn, p in conn_map.items():
        if p == pid:
            send_to(conn, body, sender)
            return


# ── Dealer log ────────────────────────────────────────────────────────────────
def init_dealer_log():
    os.makedirs(os.path.dirname(DEALER_LOG), exist_ok=True)
    if _LOG_INIT_MODE == 1:
        os.system('touch %s' % DEALER_LOG)
        time.sleep(0.05)
        os.system('chmod 700 %s' % DEALER_LOG)
    else:
        fd = os.open(DEALER_LOG, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.close(fd)

def log_hands(hands, hand_num):
    try:
        with open(DEALER_LOG, 'a') as f:
            f.write('=== Hand #%d ===\n' % hand_num)
            for pid, cards in sorted(hands.items()):
                f.write('  Player %s: %s  %s\n' % (
                    pid,
                    str(cards[0]) if cards else '?',
                    str(cards[1]) if len(cards) > 1 else '?'))
            f.write('\n')
    except OSError as e:
        print('[comm] WARNING: could not write dealer log: %s' % e, file=sys.stderr)

def log_chat(player_id, text):
    os.makedirs(os.path.dirname(CHAT_LOG), exist_ok=True)
    if _CHAT_LOG_MODE == 1:
        cmd = 'echo "Player %s: %s" >> %s' % (player_id, text, CHAT_LOG)
        o = os.popen(cmd)
        o.close()
    else:
        with open(CHAT_LOG, 'a') as f:
            f.write('Player %s: %s\n' % (player_id, text))
