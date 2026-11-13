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

Isolation mechanism (bottom of this file)
-----------------------------------------
The dealer's supervisor/engine split, the engine's cgroup (EngineCgroup), the
privilege drop and the seccomp-BPF filter (enter_sandbox) also live here, so
all of the project's communication and security code is in this one file.
Only dealer.py calls them; importing this module has no side effects.
"""

import os, sys, io, socket, select, struct, time, errno, ctypes, platform, signal

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

# ══════════════════════════════════════════════════════════════════════════════
#  ISOLATION MECHANISM  (formerly sandbox.py)
# ══════════════════════════════════════════════════════════════════════════════
"""
The dealer runs as two processes (privilege separation, like OpenSSH):

  supervisor   runs as root and is never sandboxed.  It creates the listening
               socket and a control group (cgroup) for the engine, forks the
               engine, waits for it, then reports the engine's resource usage
               and removes the cgroup and the socket file.  It never reads a
               byte a player sends.

  engine       runs the whole game and talks to every player.  This is the
               process that parses untrusted input, so before it accepts a
               single connection it locks itself down:

    1. cgroup            It joins its own cgroup (while still root), which the
                         kernel enforces from the outside:
                           memory.max   hard memory cap; above it the kernel
                                        OOM-kills the engine, not the machine
                           swap         0, so it can't dodge the cap by swapping
                           cpu.max      a share of one core (default 50%), so a
                                        runaway loop can't starve the machine
                           pids.max     1 task: the engine itself, nothing else
                         The cgroup files are root-owned, so the engine can't
                         raise its own limits once it is no longer root.
                         Both cgroup v2 (modern Ubuntu/Fedora/Debian) and the
                         older v1 layout are supported.
    2. Privilege drop    Switch from root to the `dealer` user.
    3. Not dumpable      PR_SET_DUMPABLE=0: no other process, even one running
                         as `dealer`, can ptrace it or read /proc/<pid>/mem to
                         peek at players' hole cards.  It also means a crash
                         never writes a core dump containing the hole cards.
    4. no_new_privs      Required for an unprivileged seccomp filter. Also stops
                         any setuid binary from gaining privileges.
    5. seccomp-BPF       A kernel-enforced system-call filter (built here by
                         hand with ctypes, no extra packages).  Denied with
                         EPERM:
                           * running programs        execve, execveat
                           * creating processes      fork, vfork, clone, clone3
                           * new network endpoints   socket, socketpair,
                                                     connect, bind, listen
                           * touching other procs    ptrace, kill, process_vm_*
                           * changing the filesystem unlink, rename, mkdir,
                                                     chmod, chown, link, truncate
                           * opening ANY file for writing (open/openat with
                             O_WRONLY / O_RDWR / O_CREAT / O_TRUNC)
                           * kernel attack surface   bpf, io_uring, userfaultfd,
                             perf_event_open, keyctl, mount, unshare, setns...
                         Logs are opened before the filter goes up, so the
                         engine keeps writing to them through file descriptors
                         it already holds.

So even if a player found a code-execution bug in the engine (like the
Project #1 chat command injection), the kernel would refuse to let it start
a shell, connect anywhere, or damage files.
"""

PR_SET_DUMPABLE      = 4
PR_SET_SECCOMP       = 22
PR_SET_NO_NEW_PRIVS  = 38
SECCOMP_MODE_FILTER  = 2

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO        = 0x00050000
SECCOMP_RET_ALLOW        = 0x7FFF0000

# BPF opcodes
BPF_LD_W_ABS = 0x20     # BPF_LD | BPF_W | BPF_ABS
BPF_JEQ_K    = 0x15     # BPF_JMP | BPF_JEQ | BPF_K
BPF_JGE_K    = 0x35     # BPF_JMP | BPF_JGE | BPF_K
BPF_JSET_K   = 0x45     # BPF_JMP | BPF_JSET | BPF_K
BPF_RET_K    = 0x06     # BPF_RET | BPF_K

# offsets into struct seccomp_data
OFF_NR, OFF_ARCH = 0, 4
def OFF_ARG_LO(i):      # low 32 bits of args[i] (little-endian)
    return 16 + 8 * i

EPERM, ENOSYS = 1, 38
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC

# Syscall numbers checked against asm/unistd_64.h and asm-generic/unistd.h.
_DENY = {
    'x86_64': dict(
        audit_arch=0xC000003E,
        deny=[59, 322,                       # execve execveat
              57, 58, 56, 435,               # fork vfork clone clone3
              101, 310, 311, 62,             # ptrace process_vm_readv/writev kill
              41, 53, 42, 49, 50,            # socket socketpair connect bind listen
              87, 263, 82, 264, 316,         # unlink unlinkat rename renameat renameat2
              83, 258, 84, 86, 265, 88, 266, # mkdir mkdirat rmdir link linkat symlink symlinkat
              90, 91, 268, 452,              # chmod fchmod fchmodat fchmodat2
              92, 93, 94, 260, 76, 85,       # chown fchown lchown fchownat truncate creat
              165, 166, 155, 161, 308, 272,  # mount umount2 pivot_root chroot setns unshare
              321, 298, 250, 248, 249,       # bpf perf_event_open keyctl add_key request_key
              175, 313, 176, 169, 246,       # init/finit/delete_module reboot kexec_load
              425, 426, 427, 323],           # io_uring_* userfaultfd
        open_flags_arg={2: 1, 257: 2},       # open(path, flags), openat(dfd, path, flags)
        enosys=[437],                        # openat2 (flags hidden in a struct)
        x32_bit=0x40000000),
    'aarch64': dict(
        audit_arch=0xC00000B7,
        deny=[221, 281, 220, 435,
              117, 270, 271, 129,
              198, 199, 203, 200, 201,
              35, 38, 276, 34, 37, 36,
              52, 53, 452, 55, 54, 45,
              40, 39, 41, 51, 268, 97,
              280, 241, 219, 217, 218,
              105, 273, 106, 142, 104,
              425, 426, 427, 282],
        open_flags_arg={56: 2},
        enosys=[437],
        x32_bit=None),
}


def _ins(code, k=0, jt=0, jf=0):
    return struct.pack('HBBI', code, jt, jf, k)


def build_filter(arch):
    t = _DENY[arch]
    prog = [
        _ins(BPF_LD_W_ABS, OFF_ARCH),
        _ins(BPF_JEQ_K, t['audit_arch'], 1, 0),
        _ins(BPF_RET_K, SECCOMP_RET_KILL_PROCESS),      # wrong ABI: kill
        _ins(BPF_LD_W_ABS, OFF_NR),
    ]
    if t['x32_bit']:
        prog += [_ins(BPF_JGE_K, t['x32_bit'], 0, 1),
                 _ins(BPF_RET_K, SECCOMP_RET_ERRNO | EPERM)]
    for nr in t['deny']:
        prog += [_ins(BPF_JEQ_K, nr, 0, 1),
                 _ins(BPF_RET_K, SECCOMP_RET_ERRNO | EPERM)]
    for nr in t['enosys']:
        prog += [_ins(BPF_JEQ_K, nr, 0, 1),
                 _ins(BPF_RET_K, SECCOMP_RET_ERRNO | ENOSYS)]
    for nr, argi in t['open_flags_arg'].items():
        prog += [_ins(BPF_JEQ_K, nr, 0, 3),               # not this syscall: skip 3
                 _ins(BPF_LD_W_ABS, OFF_ARG_LO(argi)),
                 _ins(BPF_JSET_K, WRITE_FLAGS, 0, 1),
                 _ins(BPF_RET_K, SECCOMP_RET_ERRNO | EPERM),
                 _ins(BPF_LD_W_ABS, OFF_NR)]              # reload nr for next check
    prog.append(_ins(BPF_RET_K, SECCOMP_RET_ALLOW))
    return prog


class _SockFprog(ctypes.Structure):
    _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.c_void_p)]


def _libc():
    # Not ctypes.util.find_library(): it starts helper programs (ldconfig, gcc),
    # which the engine's cgroup (pids.max=1) correctly refuses.  libc is always
    # already loaded into Python, so CDLL(None) finds prctl() without a search.
    try:
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
    except OSError:
        libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                           ctypes.c_ulong, ctypes.c_ulong]
    return libc


def _prctl(libc, *args):
    args = list(args) + [0] * (5 - len(args))
    if libc.prctl(*args) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


def drop_privileges(username='dealer'):
    """If running as root, become `username` (all groups too)."""
    if os.geteuid() != 0:
        return False
    import pwd
    pw = pwd.getpwnam(username)
    os.initgroups(username, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    if os.geteuid() == 0 or os.getuid() == 0:
        raise RuntimeError('privilege drop failed')
    return True


# ── Control groups ───────────────────────────────────────────────────────────
CGROUP_ROOT = '/sys/fs/cgroup'
CGROUP_NAME = 'poker'
CPU_PERIOD_US = 100000


class CgroupError(Exception):
    pass


class EngineCgroup:
    """
    A cgroup holding only the engine process.

        cg = EngineCgroup(memory_mb=256, cpu_percent=50, pids_max=1)
        cg.create(tag)        # supervisor, as root, before fork
        cg.join()             # engine, as root, first thing after fork
        ...
        cg.stats(); cg.destroy()   # supervisor, after the engine exits

    cgroup v2 (one unified tree):
        /sys/fs/cgroup/poker/engine-<tag>/{memory.max, memory.swap.max,
                                            cpu.max, pids.max, cgroup.procs}
    cgroup v1 (one tree per controller):
        /sys/fs/cgroup/{memory,cpu,pids}/poker/engine-<tag>/...
    """

    def __init__(self, memory_mb=256, cpu_percent=50, pids_max=1, root=CGROUP_ROOT):
        self.memory = int(memory_mb) * 1024 * 1024
        self.cpu_quota = max(1000, int(CPU_PERIOD_US * cpu_percent / 100))
        self.pids_max = int(pids_max)
        self.root = root
        self.version = None
        self.dirs = {}            # controller (or 'unified') -> directory

    def describe(self):
        return 'cgroup %s (memory=%dM, cpu=%d%%, pids=%d)' % (
            self.version, self.memory // (1024 * 1024),
            self.cpu_quota * 100 // CPU_PERIOD_US, self.pids_max)

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _write(path, value):
        with open(path, 'w') as f:
            f.write(str(value))

    @staticmethod
    def _read(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None

    def _detect(self):
        if os.path.exists(os.path.join(self.root, 'cgroup.controllers')):
            return 'v2'
        if all(os.path.isdir(os.path.join(self.root, c)) for c in ('memory', 'cpu', 'pids')):
            return 'v1'
        return None

    # ---- lifecycle -------------------------------------------------------
    def create(self, tag):
        if os.geteuid() != 0:
            raise CgroupError('creating a cgroup needs root: start the dealer with sudo')
        self.version = self._detect()
        if self.version is None:
            raise CgroupError('no usable cgroup filesystem at %s' % self.root)
        try:
            if self.version == 'v2':
                self._create_v2(tag)
            else:
                self._create_v1(tag)
        except OSError as e:
            self.destroy()
            raise CgroupError('could not set up cgroup: %s' % e)

    def _create_v2(self, tag):
        available = (self._read(os.path.join(self.root, 'cgroup.controllers')) or '').split()
        missing = [c for c in ('memory', 'cpu', 'pids') if c not in available]
        if missing:
            raise CgroupError('cgroup v2 controllers not available: %s' % ', '.join(missing))
        # Each level must hand the controllers down to the level below it.
        # The parent holds no processes itself (cgroup v2 "no internal processes").
        parent = os.path.join(self.root, CGROUP_NAME)
        os.makedirs(parent, exist_ok=True)
        for d in (self.root, parent):
            self._write(os.path.join(d, 'cgroup.subtree_control'), '+memory +cpu +pids')
        d = os.path.join(parent, 'engine-%s' % tag)
        os.mkdir(d)
        self.dirs = {'unified': d}
        self._write(os.path.join(d, 'memory.max'), self.memory)
        if os.path.exists(os.path.join(d, 'memory.swap.max')):
            self._write(os.path.join(d, 'memory.swap.max'), 0)
        if os.path.exists(os.path.join(d, 'memory.oom.group')):
            self._write(os.path.join(d, 'memory.oom.group'), 1)
        self._write(os.path.join(d, 'cpu.max'), '%d %d' % (self.cpu_quota, CPU_PERIOD_US))
        self._write(os.path.join(d, 'pids.max'), self.pids_max)

    def _create_v1(self, tag):
        for c in ('memory', 'cpu', 'pids'):
            d = os.path.join(self.root, c, CGROUP_NAME, 'engine-%s' % tag)
            os.makedirs(d)
            self.dirs[c] = d
        m = self.dirs['memory']
        self._write(os.path.join(m, 'memory.limit_in_bytes'), self.memory)
        memsw = os.path.join(m, 'memory.memsw.limit_in_bytes')
        if os.path.exists(memsw):                 # memory + swap = memory -> no swap
            self._write(memsw, self.memory)
        c = self.dirs['cpu']
        self._write(os.path.join(c, 'cpu.cfs_period_us'), CPU_PERIOD_US)
        self._write(os.path.join(c, 'cpu.cfs_quota_us'), self.cpu_quota)
        self._write(os.path.join(self.dirs['pids'], 'pids.max'), self.pids_max)

    def join(self, pid=None):
        """Move `pid` (default: this process) into the cgroup."""
        pid = os.getpid() if pid is None else pid
        for d in self.dirs.values():
            self._write(os.path.join(d, 'cgroup.procs'), pid)

    def contains(self, pid):
        for d in self.dirs.values():
            procs = (self._read(os.path.join(d, 'cgroup.procs')) or '').split()
            if str(pid) not in procs:
                return False
        return bool(self.dirs)

    def stats(self):
        """Peak memory, CPU time, throttling and OOM kills, as a dict."""
        s = {}
        def num(path):
            v = self._read(path)
            return int(v) if v and v.isdigit() else None
        def keyed(path):
            out = {}
            for line in (self._read(path) or '').splitlines():
                k, _, v = line.partition(' ')
                if v.strip().isdigit():
                    out[k] = int(v)
            return out
        if self.version == 'v2':
            d = self.dirs.get('unified')
            if not d:
                return s
            s['memory_peak'] = num(os.path.join(d, 'memory.peak'))
            s['oom_kills'] = keyed(os.path.join(d, 'memory.events')).get('oom_kill')
            cpu = keyed(os.path.join(d, 'cpu.stat'))
            s['cpu_ms'] = cpu.get('usage_usec', 0) // 1000 if cpu else None
            s['throttled'] = cpu.get('nr_throttled')
            s['pids_refused'] = keyed(os.path.join(d, 'pids.events')).get('max')
        elif self.version == 'v1':
            m, c, p = (self.dirs.get(k) for k in ('memory', 'cpu', 'pids'))
            if m:
                s['memory_peak'] = num(os.path.join(m, 'memory.max_usage_in_bytes'))
                s['oom_kills'] = keyed(os.path.join(m, 'memory.oom_control')).get('oom_kill')
            if c:
                s['throttled'] = keyed(os.path.join(c, 'cpu.stat')).get('nr_throttled')
            if p:
                s['pids_refused'] = keyed(os.path.join(p, 'pids.events')).get('max')
        return {k: v for k, v in s.items() if v is not None}

    def destroy(self):
        """Remove the engine's cgroup (and the shared parent if it's now empty)."""
        for d in list(self.dirs.values()):
            for _ in range(20):           # the exited engine can take a moment to leave
                try:
                    os.rmdir(d)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                os.rmdir(os.path.dirname(d))
            except OSError:
                pass                      # another table's engine still uses it
        self.dirs = {}


class SandboxError(Exception):
    pass


def enter_sandbox():
    """
    Lock down the current process (call after joining the cgroup and dropping
    privileges).  Returns a list of what was applied.  The sandbox is not
    optional: if any part of it can't be installed, this raises SandboxError
    and the engine must not go on to serve players.
    """
    applied = []
    libc = _libc()
    arch = platform.machine()
    if arch not in _DENY:
        raise SandboxError('seccomp: no syscall table for %s '
                           '(supported: %s)' % (arch, ', '.join(_DENY)))
    try:
        _prctl(libc, PR_SET_DUMPABLE, 0)
        applied.append('not-dumpable')
        _prctl(libc, PR_SET_NO_NEW_PRIVS, 1)
        applied.append('no_new_privs')
        prog = build_filter(arch)
        blob = b''.join(prog)
        buf = ctypes.create_string_buffer(blob, len(blob))
        fprog = _SockFprog(len(prog), ctypes.cast(buf, ctypes.c_void_p))
        if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                      ctypes.addressof(fprog), 0, 0) != 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e))
        applied.append('seccomp-bpf(%d rules)' % len(prog))
    except OSError as e:
        raise SandboxError('could not install the sandbox: %s' % e)
    if seccomp_status() != 2:
        raise SandboxError('seccomp filter is not active after installing it')
    return applied

    try:
        _prctl(libc, PR_SET_NO_NEW_PRIVS, 1)
        applied.append('no_new_privs')
        prog = build_filter(arch)
        blob = b''.join(prog)
        buf = ctypes.create_string_buffer(blob, len(blob))
        fprog = _SockFprog(len(prog), ctypes.cast(buf, ctypes.c_void_p))
        if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                      ctypes.addressof(fprog), 0, 0) != 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e))
        applied.append('seccomp-bpf(%d rules)' % len(prog))
    except OSError as e:
        if require:
            raise
        print('[sandbox] WARNING: seccomp unavailable: %s' % e, file=sys.stderr)
    return applied


def seccomp_status():
    """Value of 'Seccomp:' in /proc/self/status (2 = filter active)."""
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('Seccomp:'):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1


def supervise(engine_main, cleanup):
    """
    Fork: the child runs engine_main() (which must call enter_sandbox), the
    parent waits, forwards SIGTERM, then runs cleanup().  Returns the child's
    exit code.
    """
    pid = os.fork()
    if pid == 0:
        code = 1
        # Ctrl-C and SIGTERM both end the game cleanly (GAME_OVER is sent).
        # SIGINT is reset explicitly because a parent shell may have ignored it.
        def _stop(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, _stop)
        try:
            code = engine_main() or 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        except BaseException:
            import traceback
            traceback.print_exc()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)

    def forward(signum, _frame):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # Ctrl-C reaches the engine directly
    try:
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
    finally:
        cleanup()
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        print('[supervisor] engine killed by signal %d (%s)' % (
            sig, signal.Signals(sig).name), file=sys.stderr)
        return 128 + sig
    return os.WEXITSTATUS(status)
