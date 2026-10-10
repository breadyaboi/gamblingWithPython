"""
poker_communication.py  (Unix domain socket edition)
=====================================================
Core I/O library for the multiplayer poker game.

Communication model
-------------------
Named pipes used a *pair* of FIFOs per player (one each direction).
Unix domain sockets replace that with a *single bidirectional stream
socket* per player, all reached through one server socket:

    /home/dealer/sockets/game.sock    ← dealer listens here
                                         all players connect here

The dealer's event loop calls select() on:
    [ server_sock ] + [ every connected player socket ]

A readable server_sock means a new player is connecting (accept()).
A readable player socket means that player sent a message (recv()).
This is the canonical UNIX I/O-multiplexing pattern — one thread,
no blocking, N concurrent clients.

INTENTIONAL VULNERABILITIES (for OS-course attack exercises)
--------------------------------------------------------------
readVulnerability = 1   ← IMPERSONATION
    Player sends READY:<id> and the dealer trusts it.
    Any local user who can reach game.sock can connect first and
    claim to be player 0, receiving player 0's hole cards.

    Fix (readVulnerability=0): use SO_PEERCRED to read the
    connecting process's UID from the kernel, then verify it
    matches the claimed player account.  The kernel cannot lie.

readVulnerability_2 = 1  ← COMMAND INJECTION
    CHAT messages are logged via:
        os.popen('echo "%s" >> chat.log' % raw_text)
    Shell metacharacters in the message execute as the dealer.
    Example:  CHAT:hi"; cat /home/dealer/log/dealer.log > /tmp/x;"

    Fix (readVulnerability_2=0): write directly to the log file
    with Python's open(), no shell involved.

dealerLogMode = 1        ← RACE CONDITION
    init_dealer_log() calls `touch dealer.log` (creates with
    mode 0o644) then sleeps, then `chmod 700`.  A process that
    opens the file in that window keeps a valid fd after chmod.

    Fix (dealerLogMode=0): O_CREAT|O_WRONLY|O_TRUNC with mode
    0o600 — never world-readable, not even briefly.

socketVulnerability = 1  ← WORLD-ACCESSIBLE SOCKET
    game.sock and its directory are created with mode 0o777.
    Any local user can connect without being a playerN account.

    Fix (socketVulnerability=0): socket directory mode 0o750,
    game.sock mode 0o660, group=dealer — only dealer-group
    members (the playerN accounts) can connect.

TIMING SIDE CHANNEL (always present)
    Fold path is O(1).  Call/raise involve BankAccount arithmetic
    and, for raises, re-queuing all active players.  An attacker
    who measures how long a player's sent message takes to be
    echoed back (round-trip timing over the socket) can distinguish
    fold from raise and infer hand strength.
"""

import os, sys, socket, select, struct, time, pwd

# ── Configuration ────────────────────────────────────────────────────────────
SOCKET_DIR   = '/home/dealer/sockets'
SOCKET_PATH  = SOCKET_DIR + '/game.sock'
CHAT_LOG     = '/home/dealer/log/chat.log'
DEALER_LOG   = '/home/dealer/log/dealer.log'

readVulnerability    = 1   # 0=SO_PEERCRED auth  1=trust self-reported ID
readVulnerability_2  = 1   # 0=safe file write   1=VULNERABLE os.popen echo
dealerLogMode        = 1   # 0=atomic O_CREAT    1=VULNERABLE touch+chmod
socketVulnerability  = 1   # 0=restricted perms  1=VULNERABLE world-accessible


# ── Server socket lifecycle ──────────────────────────────────────────────────
def create_server_socket():
    """
    Create and bind the Unix domain server socket.

    socketVulnerability=1 (VULNERABLE):
        Directory and socket file both chmod 0o777.
        Any local user — including untrusted processes — can connect
        and impersonate a player.

    socketVulnerability=0 (SAFE):
        Directory chmod 0o750 (dealer:dealer), socket chmod 0o660.
        Only processes running as dealer or a group member can connect.
    """
    os.makedirs(SOCKET_DIR, exist_ok=True)

    # Remove stale socket from a previous run
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    srv.listen(16)
    srv.setblocking(False)

    if socketVulnerability == 1:
        # ── VULNERABLE ────────────────────────────────────────────────────
        os.chmod(SOCKET_DIR,  0o777)
        os.chmod(SOCKET_PATH, 0o777)
    else:
        # ── SAFE ──────────────────────────────────────────────────────────
        os.chmod(SOCKET_DIR,  0o750)
        os.chmod(SOCKET_PATH, 0o660)

    return srv


def accept_client(srv):
    """Accept one pending connection; return (conn_socket, None)."""
    conn, _ = srv.accept()
    conn.setblocking(False)
    return conn


# ── Authentication ────────────────────────────────────────────────────────────
def authenticate(conn, claimed_id):
    """
    Verify that the connecting process is really player<claimed_id>.

    readVulnerability=1 (VULNERABLE):
        Accepts the self-reported player ID without verification.
        An attacker who connects before the real player and sends
        READY:0 is treated as player 0.

    readVulnerability=0 (SAFE):
        Reads SO_PEERCRED from the kernel.  This returns the
        connecting process's real UID, which cannot be spoofed by
        the client.  The UID is mapped to a username and compared
        against "player<claimed_id>".
    """
    if readVulnerability == 1:
        # ── VULNERABLE ────────────────────────────────────────────────────
        return True   # trust whatever the client claimed

    # ── SAFE ──────────────────────────────────────────────────────────────
    try:
        # SO_PEERCRED returns { pid, uid, gid } as three C ints
        cred_bytes = conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED,
            struct.calcsize('3i'))
        pid, uid, gid = struct.unpack('3i', cred_bytes)
        username = pwd.getpwuid(uid).pw_name   # e.g. "player3"
        expected = 'player%d' % claimed_id
        return username == expected
    except (OSError, KeyError):
        return False


# ── Message framing ───────────────────────────────────────────────────────────
# Wire format: ":<sender>:<body>\n"
# We buffer partial reads per connection in a dict keyed by socket fd.
_recv_buf = {}

def _socket_key(conn):
    return conn.fileno()

def frame(sender, body):
    return (':%s:%s\n' % (sender, body)).encode()

def send_to(conn, body, sender='d'):
    """Send a framed message over *conn*."""
    try:
        conn.sendall(frame(sender, body))
    except OSError:
        pass

def recv_from(conn):
    """
    Read all complete '\n'-delimited messages from *conn*.
    Returns a list of (sender, body) tuples.
    Partial messages are kept in an internal buffer.
    """
    key = _socket_key(conn)
    _recv_buf.setdefault(key, b'')

    try:
        chunk = conn.recv(4096)
    except BlockingIOError:
        return []
    except OSError:
        return []

    if not chunk:
        return []   # connection closed

    _recv_buf[key] += chunk
    results = []
    while b'\n' in _recv_buf[key]:
        line, _recv_buf[key] = _recv_buf[key].split(b'\n', 1)
        text = line.decode(errors='replace').strip()
        if not text:
            continue
        parts = text.split(':', 2)
        if len(parts) >= 3:
            results.append((parts[1], parts[2]))  # (sender, body)
        else:
            results.append(('?', text))
    return results


def cleanup_conn(conn):
    key = _socket_key(conn)
    _recv_buf.pop(key, None)
    try:
        conn.close()
    except OSError:
        pass


# ── Async multiplexed receive ─────────────────────────────────────────────────
def async_recv(conn_map, server_sock=None, timeout=1.0):
    """
    select()-based multiplexed receive.

    conn_map       { socket_obj : player_id }  — existing player connections
    server_sock    if provided, also watches for new incoming connections
    timeout        seconds to wait

    Returns:
        messages    [ (player_id, body_string) ]
        new_conns   [ socket_obj ]  — sockets that just connected
                                      (only if server_sock was passed)
    """
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

    messages  = []
    new_conns = []

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


# ── Broadcast helpers ─────────────────────────────────────────────────────────
def broadcast(body, conn_map, sender='d'):
    """Send *body* to every socket in *conn_map* ({socket: player_id})."""
    for conn in conn_map:
        send_to(conn, body, sender)

def send_player(player_id, body, conn_map, sender='d'):
    """Send *body* to the socket whose value in conn_map == player_id."""
    for conn, pid in conn_map.items():
        if pid == player_id:
            send_to(conn, body, sender)
            return


# ── Dealer log ────────────────────────────────────────────────────────────────
def init_dealer_log():
    """
    Create the dealer hand log.  See module docstring for the
    race-condition vulnerability when dealerLogMode=1.
    """
    os.makedirs(os.path.dirname(DEALER_LOG), exist_ok=True)
    if dealerLogMode == 1:
        # ── VULNERABLE ────────────────────────────────────────────────────
        os.system('touch %s' % DEALER_LOG)
        time.sleep(0.05)             # exaggerate the race window
        os.system('chmod 700 %s' % DEALER_LOG)
    else:
        # ── SAFE ──────────────────────────────────────────────────────────
        fd = os.open(DEALER_LOG,
                     os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
                     0o600)
        os.close(fd)


def log_hands(hands, hand_num):
    """Append every player's hole cards to dealer.log."""
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
        print('[comm] WARNING: could not write dealer log: %s' % e,
              file=sys.stderr)


def log_chat(player_id, text):
    """
    Append a chat message to chat.log.

    readVulnerability_2=1 (VULNERABLE):
        Builds a shell command from raw *text* and passes it to
        os.popen().  Any shell metacharacter in *text* executes
        as the dealer user.
        Example injection:
            CHAT:hi"; cat /home/dealer/log/dealer.log > /tmp/stolen;"

    readVulnerability_2=0 (SAFE):
        Writes directly to the file; no shell is involved.
    """
    os.makedirs(os.path.dirname(CHAT_LOG), exist_ok=True)
    if readVulnerability_2 == 1:
        # ── VULNERABLE ────────────────────────────────────────────────────
        # text is raw, unsanitized player input
        cmd = 'echo "Player %s: %s" >> %s' % (player_id, text, CHAT_LOG)
        o = os.popen(cmd)
        o.close()
    else:
        # ── SAFE ──────────────────────────────────────────────────────────
        with open(CHAT_LOG, 'a') as f:
            f.write('Player %s: %s\n' % (player_id, text))
