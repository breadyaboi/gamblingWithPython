"""
poker_communication.py  (Unix domain socket edition)
=====================================================
Core I/O + card-rendering library shared by dealer.py and poker_client.py.

Communication model
-------------------
A single Unix domain socket at /home/dealer/sockets/game.sock.
All players connect here; the dealer's event loop uses select() on
[server_sock] + [all connected player sockets] so one thread handles
every client without blocking.

Message framing
---------------
Every message is a newline-terminated string in the format:

    :<sender>:<body>\n

  sender  — a single character or short ID ('d' for dealer, '1' for player 1)
  body    — free-form text payload (e.g. "FOLD", "RAISE:50", "CHAT:hello")

recv_from() reassembles partial TCP-style reads using a per-socket buffer
dict, then splits on newlines so callers always get complete messages.

"""

import os, sys, io, socket, select, struct, time, pwd

# ── Configuration ────────────────────────────────────────────────────────────
SOCKET_DIR  = '/home/dealer/sockets'       # directory that holds the socket file
SOCKET_PATH = SOCKET_DIR + '/game.sock'   # path clients connect to
CHAT_LOG    = '/home/dealer/log/chat.log'  # all in-game chat messages
DEALER_LOG  = '/home/dealer/log/dealer.log'  # each hand's hole cards (dealer-only)

# Mode flags — each controls one aspect of the server's behaviour.
# Flip a flag from 1 to 0 to switch to the alternative implementation.
_AUTH_MODE      = 1   # 1 = trust the player_id the client reports
                      # 0 = verify via SO_PEERCRED (kernel-supplied UID)
_CHAT_LOG_MODE  = 1   # 1 = write chat to file via os.popen() shell command
                      # 0 = write chat to file directly with open()
_LOG_INIT_MODE  = 1   # 1 = create dealer log with os.system('touch ...')
                      # 0 = create dealer log atomically with os.open()
_SOCKET_MODE    = 1   # 1 = set socket directory and file permissions to 0o777
                      # 0 = restrict to 0o750 / 0o660 (dealer group only)


# ── Card rendering ────────────────────────────────────────────────────────────
# Import Deck from cards.py so we can use its translate_rank() / translate_suit()
# methods and its create_hand() / create_concealed_hand() drawing functions.
from cards import Deck as _Deck, Card

# Create one shared Deck instance solely for access to its rendering helpers.
# We never draw cards from this deck — it is only used for translate_* and create_*.
_deck = _Deck()


def _capture(fn, *args):
    """
    Call fn(*args), capture everything it writes to sys.stdout, and return
    the output as a list of non-empty lines.

    cards.py's create_hand() and create_concealed_hand() write directly to
    sys.stdout rather than returning strings.  This helper redirects stdout
    to a StringIO buffer so the output can be used programmatically.
    """
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        fn(*args)
    finally:
        sys.stdout = old
    return [line for line in buf.getvalue().split('\n') if line]


def render_cards(cards):
    """
    Return a list of 5 strings representing a horizontal row of face-up cards
    in ASCII art, produced by cards.py's Deck.create_hand().
    """
    if not cards:
        return []
    return _capture(_deck.create_hand, cards)


def render_concealed(n):
    """
    Return ASCII art for n face-down cards (backs only — rank and suit hidden),
    produced by cards.py's Deck.create_concealed_hand().
    """
    if n <= 0:
        return []
    # create_concealed_hand iterates over the list — pass n placeholder objects
    return _capture(_deck.create_concealed_hand, [None] * n)


def print_cards(cards, label=''):
    """
    Print graphical card art to stdout using cards.py's Deck.create_hand(),
    with an optional header label above the art.
    """
    if label:
        print(label)
    _deck.create_hand(cards)   # writes directly to stdout
    print()


def parse_card(text):
    """
    Parse a 'Rank of Suit' string (the format Card.__str__() produces) back
    into a Card object.  Returns None on malformed input so callers can skip
    bad tokens without crashing.

    Example:  parse_card('Ace of Hearts')  →  Card(suit='Hearts', rank='Ace')
    """
    # Card is imported at the top of this module from cards.py
    text = text.strip()
    if ' of ' in text:
        rank, suit = text.split(' of ', 1)
        return Card(suit=suit.strip(), rank=rank.strip())
    return None


def parse_cards(text, sep='|'):
    """
    Parse a delimiter-separated string of 'Rank of Suit' tokens into a list
    of Card objects.  Tokens that don't parse are silently skipped.

    Example:  parse_cards('Ace of Hearts|King of Spades')
              → [Card('Hearts','Ace'), Card('Spades','King')]
    """
    result = []
    for token in text.split(sep):
        c = parse_card(token)
        if c:
            result.append(c)
    return result


# ── Server socket lifecycle ───────────────────────────────────────────────────
def create_server_socket():
    """
    Create and bind the Unix domain socket that all players connect to.

    The socket directory and file are created fresh each run (any leftover
    socket file from a crashed previous session is removed first).
    The socket is set to non-blocking so the dealer's select() loop never
    stalls waiting for a single slow client.
    """
    os.makedirs(SOCKET_DIR, exist_ok=True)

    # Remove a stale socket file left over from a previous run
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass  # nothing to remove — that's fine

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    srv.listen(16)        # allow up to 16 queued connection attempts
    srv.setblocking(False)

    # Set permissions on the socket directory and file
    if _SOCKET_MODE == 1:
        os.chmod(SOCKET_DIR,  0o777)
        os.chmod(SOCKET_PATH, 0o777)
    else:
        # Restrict access: dealer group can connect, others cannot
        os.chmod(SOCKET_DIR,  0o750)
        os.chmod(SOCKET_PATH, 0o660)

    return srv


def accept_client(srv):
    """
    Accept one pending connection from the server socket and return it.
    Sets the new socket to non-blocking so recv() never hangs.
    """
    conn, _ = srv.accept()
    conn.setblocking(False)
    return conn


# ── Authentication ────────────────────────────────────────────────────────────
def authenticate(conn, claimed_id):
    """
    Decide whether the player on conn is allowed to identify as claimed_id.

    Mode 1 (_AUTH_MODE == 1):
        Accept the claimed ID without verification.  Any client that can
        reach the socket can claim any player number.

    Mode 0 (_AUTH_MODE == 0):
        Use SO_PEERCRED to ask the kernel for the connecting process's real
        UID, then look up its username in /etc/passwd.  The client must be
        running as the matching playerN Unix account.

    SO_PEERCRED is a Linux socket option that returns the PID, UID, and GID
    of the process on the other end of a Unix domain socket — these values
    come from the kernel and cannot be forged by the client.
    """
    if _AUTH_MODE == 1:
        return True   # trust whatever the client says
    try:
        # getsockopt returns 3 packed ints: pid, uid, gid
        cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                               struct.calcsize('3i'))
        pid, uid, gid = struct.unpack('3i', cred)
        username = pwd.getpwuid(uid).pw_name   # resolve UID → username
        return username == ('player%d' % claimed_id)
    except (OSError, KeyError):
        return False   # couldn't read credentials — deny access


# ── Message framing ───────────────────────────────────────────────────────────
# Per-socket receive buffers.  Unix domain sockets are stream-oriented, so a
# single recv() call might return a partial message or multiple messages joined
# together.  We accumulate bytes here and split on '\n' boundaries.
_recv_buf = {}

def frame(sender, body):
    """
    Encode a message as  :<sender>:<body>\n  ready to send over the socket.
    The leading colon makes it easy to split with text.split(':', 2).
    """
    return (':%s:%s\n' % (sender, body)).encode()

def send_to(conn, body, sender='d'):
    """
    Send one framed message to a single connection.
    Silently ignores OSError (e.g. client disconnected mid-send).
    """
    try:
        conn.sendall(frame(sender, body))
    except OSError:
        pass

def recv_from(conn):
    """
    Read all available data from conn and return a list of (sender, body)
    tuples for every complete newline-terminated message found.

    Partial messages at the end of the buffer are kept for the next call.
    Returns an empty list if no complete message is available yet, or if
    the connection was closed / would block.
    """
    key = conn.fileno()
    _recv_buf.setdefault(key, b'')   # create an empty buffer on first use
    try:
        chunk = conn.recv(4096)
    except (BlockingIOError, OSError):
        return []   # nothing ready to read right now
    if not chunk:
        return []   # empty read means the remote end closed the connection
    _recv_buf[key] += chunk

    results = []
    # Extract every complete line from the buffer
    while b'\n' in _recv_buf[key]:
        line, _recv_buf[key] = _recv_buf[key].split(b'\n', 1)
        text = line.decode(errors='replace').strip()
        if not text:
            continue
        # Expected format: :<sender>:<body>
        parts = text.split(':', 2)
        results.append((parts[1] if len(parts) >= 3 else '?',
                        parts[2] if len(parts) >= 3 else text))
    return results

def cleanup_conn(conn):
    """
    Remove the socket's receive buffer and close the connection gracefully.
    Safe to call multiple times; OSError on close is ignored.
    """
    _recv_buf.pop(conn.fileno(), None)
    try:
        conn.close()
    except OSError:
        pass


# ── Async multiplexed receive ─────────────────────────────────────────────────
def async_recv(conn_map, server_sock=None, timeout=1.0):
    """
    Wait up to `timeout` seconds for activity on any socket in conn_map,
    optionally including the server socket for new connections.

    Uses select() — a POSIX system call that returns when one or more file
    descriptors become readable, avoiding busy-polling or blocking threads.

    Parameters
    ----------
    conn_map    : {socket: player_id}  — active player connections to watch
    server_sock : the listening socket, or None to skip new-connection handling
    timeout     : how long (seconds) to wait before returning empty lists

    Returns
    -------
    messages  : [(player_id, body), ...]  — decoded messages from players
    new_conns : [socket, ...]             — freshly accepted connections
    """
    watch = list(conn_map.keys())
    if server_sock is not None:
        watch.append(server_sock)
    if not watch:
        time.sleep(timeout)   # nothing to watch — just wait out the timeout
        return [], []
    try:
        # select() blocks until at least one socket has data (or timeout elapses)
        readable, _, _ = select.select(watch, [], watch, timeout)
    except (ValueError, OSError):
        return [], []

    messages, new_conns = [], []
    for s in readable:
        if server_sock is not None and s is server_sock:
            # A new player is knocking — accept the connection
            new_conns.append(accept_client(server_sock))
        else:
            player = conn_map.get(s)
            if player is None:
                continue
            # Read all complete messages from this player's socket
            for sender, body in recv_from(s):
                messages.append((player, body))
    return messages, new_conns


def broadcast(body, conn_map, sender='d'):
    """Send the same message to every connection in conn_map."""
    for conn in conn_map:
        send_to(conn, body, sender)

def send_player(pid, body, conn_map, sender='d'):
    """Send a message to one specific player identified by player ID."""
    for conn, p in conn_map.items():
        if p == pid:
            send_to(conn, body, sender)
            return


# ── Dealer log ────────────────────────────────────────────────────────────────
def init_dealer_log():
    """
    Create the dealer log file before the game begins.

    Mode 1 (_LOG_INIT_MODE == 1):
        Uses os.system('touch ...') to create the file, then chmod's it.
        There is a brief window between creation and the chmod call during
        which the file exists with default umask permissions.

    Mode 0 (_LOG_INIT_MODE == 0):
        Creates the file atomically with os.open() and mode 0o600 in one
        syscall, so it is never readable by anyone but the owner.
    """
    os.makedirs(os.path.dirname(DEALER_LOG), exist_ok=True)
    if _LOG_INIT_MODE == 1:
        os.system('touch %s' % DEALER_LOG)
        time.sleep(0.05)
        os.system('chmod 700 %s' % DEALER_LOG)
    else:
        # O_CREAT | O_WRONLY | O_TRUNC: create (or truncate) with exact permissions
        fd = os.open(DEALER_LOG, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.close(fd)

def log_hands(hands, hand_num):
    """
    Append every player's hole cards to the dealer log file.
    This file is only readable by the dealer account and records
    all hands for post-game review.
    """
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
    """
    Append a chat message to the chat log file.

    Mode 1 (_CHAT_LOG_MODE == 1):
        Passes player_id and text directly into a shell command string via
        os.popen().  The text argument is not sanitised before being
        interpolated into the command.

    Mode 0 (_CHAT_LOG_MODE == 0):
        Writes to the file directly with Python's open(), no shell involved.
    """
    os.makedirs(os.path.dirname(CHAT_LOG), exist_ok=True)
    if _CHAT_LOG_MODE == 1:
        # Build a shell command that appends the chat line to the log file
        cmd = 'echo "Player %s: %s" >> %s' % (player_id, text, CHAT_LOG)
        o = os.popen(cmd)
        o.close()
    else:
        with open(CHAT_LOG, 'a') as f:
            f.write('Player %s: %s\n' % (player_id, text))
