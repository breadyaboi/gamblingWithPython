"""
poker_communication.py  (Project #2 — hardened Unix domain socket edition)
==========================================================================
The single communications library shared by dealer.py and poker_client.py.
Every byte that moves between processes in this game goes through this file.

Communication model
-------------------
One Unix domain stream socket at /var/lib/poker/game.sock.  Only members of
the `pokerplayers` group can reach it (directory 2750, socket 0660).

The dealer's `Server` object runs a select() event loop over the listening
socket plus every client socket, for both reading AND writing, so a single
thread serves every player without ever blocking on any one of them.

Message framing
---------------
Every message is one newline-terminated line:

    :<sender>:<body>\n

The server ignores the <sender> field a client writes: a client's identity
comes only from the kernel (SO_PEERCRED), never from anything it sends.

Message routing
---------------
    dealer  -> one player      Server.send(pid, ...)        hole cards, YOUR_TURN
    dealer  -> everyone        Server.broadcast(...)        board, pot, winners
    player  -> everyone        CHAT:<text>                  relayed by the library
    player  -> one player      MSG:<pid>:<text>             private message, relayed
    player  -> dealer          FOLD / CALL / RAISE:n ...    surfaced as events

Robustness / isolation between players (new in Project #2)
----------------------------------------------------------
* Bounded input:  a line longer than MAX_LINE bytes, or a partial line that
  grows past MAX_LINE without a newline, gets the connection dropped.  Each
  connection can hold at most MAX_LINE + RECV_CHUNK bytes of input in memory.
* Bounded output: replies are queued per connection and written only when the
  socket is writable.  A client that stops reading has its queue capped at
  MAX_OUTBUF and is then dropped, so it can never stall the dealer.
* Rate limits:   token buckets on every message and, more tightly, on chat.
* Auth deadline: a connection that hasn't authenticated within AUTH_TIMEOUT
  seconds is dropped, and at most MAX_PENDING unauthenticated connections are
  held at once.
* Sanitising:    chat text is stripped of control characters (ANSI escape
  sequences, carriage returns...) so one player can't take over another
  player's terminal.
* Disconnects become explicit ('leave', pid, reason) events.  The game then
  folds that one player instead of guessing from an empty read.
"""

import os, sys, io, socket, select, struct, time, errno

# ── Configuration ────────────────────────────────────────────────────────────
SOCKET_DIR   = '/var/lib/poker'               # 2750 dealer:pokerplayers
SOCKET_PATH  = SOCKET_DIR + '/game.sock'      # 0660 dealer:pokerplayers
LOG_DIR      = '/home/dealer/log'             # 0700 dealer only
CHAT_LOG     = LOG_DIR + '/chat.log'
DEALER_LOG   = LOG_DIR + '/dealer.log'
PLAYER_GROUP = 'pokerplayers'

MAX_LINE      = 512          # bytes, including the trailing newline
RECV_CHUNK    = 4096
MAX_OUTBUF    = 64 * 1024    # bytes queued for a client before we give up on it
MAX_CHAT_LEN  = 200          # characters of chat text after sanitising
AUTH_TIMEOUT  = 10.0         # seconds to send READY:<id>
MAX_PENDING   = 8            # unauthenticated connections held at once
MSG_RATE      = (10.0, 30)   # all messages: 10/s sustained, burst of 30
CHAT_RATE     = (0.5, 5)     # chat + DMs:   1 per 2 s sustained, burst of 5


# ── Card rendering (unchanged API) ───────────────────────────────────────────
from cards import Deck as _Deck, Card

_deck = _Deck()   # only used for its rendering helpers, never drawn from


def _capture(fn, *args):
    """Run fn(*args), capture what it writes to stdout, return non-empty lines."""
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        fn(*args)
    finally:
        sys.stdout = old
    return [line for line in buf.getvalue().split('\n') if line]


def render_cards(cards):
    """ASCII art (list of 5 strings) for a row of face-up cards."""
    if not cards:
        return []
    return _capture(_deck.create_hand, cards)


def render_concealed(n):
    """ASCII art for n face-down cards."""
    if n <= 0:
        return []
    return _capture(_deck.create_concealed_hand, [None] * n)


def parse_card(text):
    """'Ace of Hearts' -> Card, or None if it doesn't look like a valid card."""
    text = text.strip()
    if ' of ' not in text:
        return None
    rank, suit = (s.strip() for s in text.split(' of ', 1))
    if rank not in _Deck.ranks or suit not in _Deck.suits:
        return None
    return Card(suit=suit, rank=rank)


def parse_cards(text, sep='|'):
    return [c for c in (parse_card(t) for t in text.split(sep)) if c]


# ── Text helpers ─────────────────────────────────────────────────────────────
def clean_text(text, limit=MAX_CHAT_LEN):
    """Keep printable characters only (drops ESC, \\r, \\b, NUL...), then truncate."""
    return ''.join(ch for ch in text if ch.isprintable())[:limit].strip()


def frame(sender, body):
    """Encode  :<sender>:<body>\\n.  Newlines in body are flattened so one call = one line."""
    body = str(body).replace('\r', ' ').replace('\n', ' ')
    return (':%s:%s\n' % (sender, body)).encode('utf-8', errors='replace')


def unframe(line):
    """bytes line (no newline) -> (sender, body).  Malformed lines get sender '?'."""
    text = line.decode('utf-8', errors='replace').strip()
    parts = text.split(':', 2)
    if len(parts) == 3 and parts[0] == '':
        return parts[1], parts[2]
    return '?', text


# ── Secure append-only log files ─────────────────────────────────────────────
class SecureLog:
    """
    An append-only log opened ONCE, before the dealer enters its sandbox.

    Replaces the old  os.popen('echo "Player %s: %s" >> chat.log')  which let
    any player run shell commands as the dealer with  CHAT:"; rm -rf ~ #  ,
    and the old  touch + chmod  pair, which left a window where the hole-card
    log existed with default (world-readable) permissions.

    O_NOFOLLOW refuses to open a symlink planted in place of the log, and mode
    0600 is applied atomically at creation.
    """
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        os.fchmod(fd, 0o600)          # in case it already existed with looser perms
        self._f = os.fdopen(fd, 'a', buffering=1, encoding='utf-8')

    def write(self, line):
        try:
            self._f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'),
                                       clean_text(line, 1000)))
        except OSError as e:
            print('[comm] log write failed: %s' % e, file=sys.stderr)

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass


# ── Server socket lifecycle ──────────────────────────────────────────────────
def create_server_socket(path=SOCKET_PATH, owner_uid=-1, group_gid=-1):
    """
    Bind the listening socket.  Only the dealer and pokerplayers group can use it:
    the directory is 2750 (set up by the installer) and the socket is 0660.
    (Project #1 used 0777 on both, so any local user could connect.)
    """
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    try:
        os.unlink(path)               # stale socket from a crashed run
    except FileNotFoundError:
        pass
    old_umask = os.umask(0o117)       # socket is born 0660, no window at 0777
    try:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
    finally:
        os.umask(old_umask)
    # The umask above already made it 0660.  No chmod afterwards: the dealer is
    # started as root, and chmod follows symlinks, so a file swapped in after
    # bind() could trick root into chmod-ing something else.  lchown never follows.
    if owner_uid != -1 or group_gid != -1:
        os.chown(path, owner_uid, group_gid, follow_symlinks=False)
    srv.listen(16)
    srv.setblocking(False)
    return srv


def peer_uid(conn):
    """UID of the process on the other end, from the kernel (cannot be forged)."""
    cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                           struct.calcsize('3i'))
    _pid, uid, _gid = struct.unpack('3i', cred)
    return uid


class _TokenBucket:
    def __init__(self, rate, burst):
        self.rate, self.burst = rate, burst
        self.tokens, self.t = float(burst), time.monotonic()

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class _Conn:
    __slots__ = ('sock', 'pid', 'inbuf', 'outbuf', 'born', 'msg_bucket',
                 'chat_bucket', 'dead')

    def __init__(self, sock):
        self.sock = sock
        self.pid = None               # set once authenticated
        self.inbuf = b''
        self.outbuf = bytearray()
        self.born = time.monotonic()
        self.msg_bucket = _TokenBucket(*MSG_RATE)
        self.chat_bucket = _TokenBucket(*CHAT_RATE)
        self.dead = False


# ── The dealer-side server ───────────────────────────────────────────────────
class Server:
    """
    Owns every client connection.  The dealer only ever talks in player IDs;
    sockets never leak out of this class.

    uid_map   {uid: player_id}  the seats, resolved before the sandbox goes up
    chat_log  optional SecureLog for CHAT/MSG traffic
    """

    def __init__(self, srv_sock, uid_map, chat_log=None):
        self.srv = srv_sock
        self.uid_map = dict(uid_map)
        self.chat_log = chat_log
        self.conns = {}               # socket -> _Conn  (pending + authenticated)
        self.by_pid = {}              # player_id -> _Conn
        self._events = []             # events produced outside poll()

    # ---- queries ---------------------------------------------------------
    def connected(self):
        return sorted(self.by_pid)

    def is_connected(self, pid):
        return pid in self.by_pid

    # ---- sending ---------------------------------------------------------
    def _queue(self, c, data):
        if c.dead:
            return
        c.outbuf += data
        self._flush(c)
        if not c.dead and len(c.outbuf) > MAX_OUTBUF:
            self._drop(c, 'not reading messages (output queue full)')

    def _flush(self, c):
        while c.outbuf and not c.dead:
            try:
                n = c.sock.send(c.outbuf)
            except (BlockingIOError, InterruptedError):
                return                # kernel buffer full; finish when writable
            except OSError:
                self._drop(c, 'connection lost')
                return
            del c.outbuf[:n]

    def send(self, pid, body, sender='d'):
        c = self.by_pid.get(pid)
        if c:
            self._queue(c, frame(sender, body))

    def broadcast(self, body, pids=None, exclude=(), sender='d'):
        """Send to every authenticated player (or just `pids`), minus `exclude`."""
        data = frame(sender, body)
        targets = self.connected() if pids is None else list(pids)
        for pid in targets:
            if pid in exclude:
                continue
            c = self.by_pid.get(pid)
            if c:
                self._queue(c, data)

    # ---- connection management ------------------------------------------
    def _drop(self, c, reason):
        if c.dead:
            return
        c.dead = True
        if c.outbuf:                  # best effort: tell them why
            try:
                c.sock.send(bytes(c.outbuf[:MAX_OUTBUF]))
            except OSError:
                pass
        try:
            c.sock.send(frame('d', 'ERROR:Disconnected: %s' % reason))
        except OSError:
            pass
        try:
            c.sock.close()
        except OSError:
            pass
        self.conns.pop(c.sock, None)
        if c.pid is not None and self.by_pid.get(c.pid) is c:
            del self.by_pid[c.pid]
            self._events.append(('leave', c.pid, reason))

    def kick(self, pid, reason):
        c = self.by_pid.get(pid)
        if c:
            self._drop(c, reason)

    def _accept(self):
        while True:
            try:
                sock, _ = self.srv.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            pending = sum(1 for c in self.conns.values() if c.pid is None)
            if pending >= MAX_PENDING:
                sock.close()
                continue
            sock.setblocking(False)
            c = _Conn(sock)
            self.conns[sock] = c
            self._queue(c, frame('d', 'IDENTIFY:Send READY:<player_id>'))

    def _authenticate(self, c, body):
        try:
            claimed = int(body.split(':', 1)[1])
        except (ValueError, IndexError):
            self._queue(c, frame('d', 'ERROR:Use READY:<number>'))
            return
        try:
            uid = peer_uid(c.sock)
        except OSError:
            self._drop(c, 'could not read peer credentials')
            return
        real = self.uid_map.get(uid)
        if real is None or real != claimed:
            self._drop(c, 'auth failed for player%d' % claimed)
            return
        if real in self.by_pid:
            self._drop(c, 'player %d is already connected' % real)
            return
        c.pid = real
        self.by_pid[real] = c
        self._events.append(('join', real))

    # ---- player-to-player relays ----------------------------------------
    def _relay(self, c, upper, body):
        if not c.chat_bucket.take():
            self._queue(c, frame('d', 'ERROR:Slow down — chat is rate limited.'))
            return
        if upper.startswith('CHAT:'):
            text = clean_text(body[5:])
            if not text:
                return
            if self.chat_log:
                self.chat_log.write('P%d -> all: %s' % (c.pid, text))
            self.broadcast('CHAT:[P%d] %s' % (c.pid, text))
            return
        # MSG:<pid>:<text>
        try:
            _, target, text = body.split(':', 2)
            target = int(target)
        except ValueError:
            self._queue(c, frame('d', 'ERROR:Use MSG:<player_id>:<text>'))
            return
        text = clean_text(text)
        if target not in self.by_pid:
            self._queue(c, frame('d', 'ERROR:Player %d is not connected.' % target))
            return
        if not text:
            return
        if self.chat_log:
            self.chat_log.write('P%d -> P%d: %s' % (c.pid, target, text))
        self.send(target, 'DM:[P%d -> you] %s' % (c.pid, text))
        self.send(c.pid, 'DM:[you -> P%d] %s' % (target, text))

    # ---- reading ---------------------------------------------------------
    def _on_readable(self, c):
        try:
            chunk = c.sock.recv(RECV_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(c, 'connection lost')
            return
        if not chunk:
            self._drop(c, 'disconnected')
            return
        c.inbuf += chunk
        while not c.dead:
            nl = c.inbuf.find(b'\n')
            if nl < 0:
                break
            line, c.inbuf = c.inbuf[:nl], c.inbuf[nl + 1:]
            if nl + 1 > MAX_LINE:
                self._drop(c, 'message longer than %d bytes' % MAX_LINE)
                return
            if not c.msg_bucket.take():
                self._drop(c, 'flooding (too many messages)')
                return
            _sender, body = unframe(line)
            body = body.strip()
            if not body:
                continue
            upper = body.upper()
            if c.pid is None:
                if upper.startswith('READY:'):
                    self._authenticate(c, body)
                else:
                    self._queue(c, frame('d', 'ERROR:Authenticate first: READY:<id>'))
            elif upper.startswith('CHAT:') or upper.startswith('MSG:'):
                self._relay(c, upper, body)
            else:
                self._events.append(('msg', c.pid, clean_text(body, MAX_LINE)))
        # The peer-review bug: text with no newline used to pile up forever.
        if not c.dead and len(c.inbuf) >= MAX_LINE:
            self._drop(c, 'message longer than %d bytes' % MAX_LINE)

    def poll(self, timeout):
        """
        Wait up to `timeout` seconds for activity, then return a list of events:
            ('join',  pid)
            ('leave', pid, reason)
            ('msg',   pid, body)      game commands only; chat/DMs are relayed here
        Returns as soon as there is at least one event.
        """
        if self._events:
            timeout = 0
        now = time.monotonic()
        for c in list(self.conns.values()):          # enforce auth deadline
            if c.pid is None and now - c.born > AUTH_TIMEOUT:
                self._drop(c, 'did not authenticate in time')
        rlist = [self.srv] + list(self.conns)
        wlist = [c.sock for c in self.conns.values() if c.outbuf]
        try:
            readable, writable, _ = select.select(rlist, wlist, [], max(0, timeout))
        except InterruptedError:
            readable, writable = [], []
        for s in writable:
            c = self.conns.get(s)
            if c:
                self._flush(c)
        for s in readable:
            if s is self.srv:
                self._accept()
            else:
                c = self.conns.get(s)
                if c and not c.dead:
                    self._on_readable(c)
        events, self._events = self._events, []
        return events

    def flush_all(self, timeout=1.0):
        """Try to deliver everything queued (used right before shutdown)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            pending = [c for c in self.conns.values() if c.outbuf]
            if not pending:
                return
            _, w, _ = select.select([], [c.sock for c in pending], [],
                                    max(0, end - time.monotonic()))
            for s in w:
                c = self.conns.get(s)
                if c:
                    self._flush(c)

    def close(self):
        self.flush_all()
        for c in list(self.conns.values()):
            try:
                c.sock.close()
            except OSError:
                pass
        self.conns.clear()
        self.by_pid.clear()
        try:
            self.srv.close()
        except OSError:
            pass
