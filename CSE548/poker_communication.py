"""
poker_communication.py
======================
Core I/O library for the multiplayer poker game.

Architecture mirrors Virtual Werewolves (communication.py) but uses
select.select() for asynchronous multiplexed I/O instead of one blocking
thread per player.

INTENTIONAL VULNERABILITIES (for OS-course side-channel/attack exercises):
  readVulnerability   = 1  ->  recv() uses os.popen('cat pipe')
                               The cat process is visible in `ps aux`,
                               revealing *which* pipe is being read.
  readVulnerability_2 = 1  ->  send() builds a shell command with raw
                               player input and passes it to os.popen().
                               Any player message containing shell
                               metacharacters is *executed by the dealer*.
                               Example injection: "fold; cat /home/dealer/log/dealer.log"
  dealerLogMode       = 1  ->  init_dealer_log() calls `touch` then
                               `chmod 700` as two separate steps.
                               There is a race-condition window between
                               creation (mode 0o644) and restriction
                               (mode 0o700) where any local user can
                               read the newly created log file.

Pipe layout (all paths under pipeRoot):
  Player N -> Dealer:  <N>todD/<N>tod   (player writes, dealer reads)
  Dealer -> Player N:  dto<N>D/dto<N>   (dealer writes, player reads)
"""

import os, sys, select, time

# ── Configuration ───────────────────────────────────────────────────────────
pipeRoot         = '/home/dealer/pipes/'
readVulnerability   = 1   # 0 = safe direct read,   1 = VULNERABLE os.popen cat
readVulnerability_2 = 1   # 0 = safe direct write,  1 = VULNERABLE os.popen echo (injection)
dealerLogMode       = 1   # 0 = atomic O_CREAT,     1 = VULNERABLE touch + chmod

# ── Pipe path helpers ────────────────────────────────────────────────────────
def _p2d_path(player):
    """Path of the player→dealer FIFO."""
    return '%s%stodD/%stod' % (pipeRoot, player, player)

def _d2p_path(player):
    """Path of the dealer→player FIFO."""
    return '%sdto%sD/dto%s' % (pipeRoot, player, player)


# ── Send (dealer → player) ───────────────────────────────────────────────────
def send(receiver, msg, sender='d'):
    """
    Send *msg* to *receiver* (a player number) from *sender*.

    readVulnerability_2=1 (VULNERABLE):
        Builds a shell command string containing raw *msg* and passes it
        directly to os.popen().  A player who sends:
            fold; cp /home/dealer/log/dealer.log /tmp/stolen.txt
        will have the cp command executed as the dealer user.

    readVulnerability_2=0 (SAFE):
        Writes directly to the FIFO file descriptor; no shell is involved.
    """
    pipe = str(receiver)
    if readVulnerability_2 == 1:
        # ── VULNERABLE ────────────────────────────────────────────────────
        # msg is raw, unsanitized player input.  Any shell metacharacter
        # in msg (; & | ` $(...) etc.) is interpreted by /bin/sh.
        #
        # Original safe version (disabled in werewolves too):
        #   safe_msg = msg.replace("'","").replace(";","").replace("&","")
        raw = "(echo :%s:%s > %sdto%sD/dto%s) 2> /dev/null &" % (
            sender, msg, pipeRoot, pipe, pipe)
        o = os.popen(raw)
        o.close()
    else:
        # ── SAFE ──────────────────────────────────────────────────────────
        try:
            fd = os.open(_d2p_path(pipe),
                         os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, (':%s:%s\n' % (sender, msg)).encode())
            os.close(fd)
        except OSError:
            pass


# ── Blocking recv (single player) ────────────────────────────────────────────
def recv(player):
    """
    Blocking read from a single player pipe.  Used only during the
    connection-handshake phase before the async loop starts.

    readVulnerability=1 (VULNERABLE):
        Spawns `cat <pipe_path>` as a child process.
        `ps aux` reveals the exact pipe being read, identifying which
        player the dealer is currently waiting on.

    readVulnerability=0 (SAFE):
        Opens the FIFO directly; no child process is created.
    """
    pipe = str(player)
    if readVulnerability == 1:
        # ── VULNERABLE ────────────────────────────────────────────────────
        # Child process `cat /home/dealer/pipes/NtodD/Ntod` is visible in
        # the process table to any local user via `ps aux`.
        o = os.popen('cat %s' % _p2d_path(pipe))
        data = o.read().strip()
        o.close()
        return data
    else:
        # ── SAFE ──────────────────────────────────────────────────────────
        fd = os.open(_p2d_path(pipe), os.O_RDONLY)
        data = os.read(fd, 4096).decode().strip()
        os.close(fd)
        return data


# ── Async recv (multiple players via select) ─────────────────────────────────
def open_player_pipes(players):
    """
    Open every player→dealer FIFO with O_RDONLY | O_NONBLOCK.

    O_NONBLOCK means open() returns immediately even if no writer is
    present yet (unlike a plain open() which would block).  The returned
    file descriptors are suitable for select.select().

    Returns fd_map: { fd_int : player_id }
    """
    fd_map = {}
    for p in players:
        path = _p2d_path(str(p))
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            fd_map[fd] = p
        except OSError as e:
            print('[comm] WARNING: cannot open pipe for player %s: %s' % (p, e),
                  file=sys.stderr)
    return fd_map


def async_recv(fd_map, timeout=1.0):
    """
    Non-blocking multiplexed receive using select.select().

    Waits up to *timeout* seconds for any player to write a message.
    Returns a list of (player_id, message_string) for every pipe that
    had data ready.

    This replaces the per-player blocking threads used in Virtual
    Werewolves — a single event-loop iteration handles all N players.

    TIMING SIDE CHANNEL:
        The dealer's processing time after receiving a message varies by
        action type:
          - FOLD   → O(1) list removal; no BankAccount lookup
          - CALL   → BankAccount.show_balance() + sub_balance() arithmetic
          - RAISE  → BankAccount arithmetic + re-queue all active players
        An attacker watching when this pipe's writer-side FIFO transitions
        from "open()" wait state to "read()" wait state can distinguish
        fold from raise, leaking hand strength information.
    """
    if not fd_map:
        time.sleep(timeout)
        return []
    try:
        readable, _, _ = select.select(list(fd_map.keys()), [], [], timeout)
    except (ValueError, OSError):
        return []

    results = []
    for fd in readable:
        try:
            raw = os.read(fd, 4096)
            if not raw:
                continue  # EOF (pipe writer closed)
            data = raw.decode(errors='replace').strip()
            if not data:
                continue
            # Wire format: :<sender>:<message>
            parts = data.split(':', 2)
            message = parts[2] if len(parts) >= 3 else data
            results.append((fd_map[fd], message))
        except BlockingIOError:
            pass  # O_NONBLOCK: no data available right now
        except OSError:
            pass
    return results


def close_pipes(fd_map):
    """Close all file descriptors in an fd_map."""
    for fd in list(fd_map.keys()):
        try:
            os.close(fd)
        except OSError:
            pass


# ── Broadcast ────────────────────────────────────────────────────────────────
def broadcast(msg, players, sender='d'):
    """
    Send *msg* to every player in *players* (dict or iterable of player IDs).
    """
    for p in players:
        send(p, msg, sender)


# ── Dealer log (the target for attacks) ──────────────────────────────────────
DEALER_LOG = '/home/dealer/log/dealer.log'

def init_dealer_log():
    """
    Create the dealer's hand log.

    dealerLogMode=1 (VULNERABLE):
        Step 1: `touch dealer.log`   → file created with umask-derived mode
                                       (typically 0o644 — world-readable!)
        Step 2: `chmod 700`          → now restricted to dealer only

        A local user who races between steps 1 and 2 can open() the file
        for reading before chmod runs, keeping the fd valid even after the
        permissions are tightened (Linux does not revoke open fds on
        permission change).

    dealerLogMode=0 (SAFE):
        Creates the file in a single O_CREAT | O_WRONLY | O_EXCL call
        with mode 0o600 — the file is never world-readable, even briefly.
    """
    if dealerLogMode == 1:
        # ── VULNERABLE ────────────────────────────────────────────────────
        os.system('touch %s' % DEALER_LOG)
        time.sleep(0.05)          # exaggerate the race window for the lab
        os.system('chmod 700 %s' % DEALER_LOG)
    else:
        # ── SAFE ──────────────────────────────────────────────────────────
        fd = os.open(DEALER_LOG,
                     os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
                     0o600)
        os.close(fd)


def log_hands(hands, hand_num):
    """
    Append every player's hole cards to the dealer log.

    This is the *secret* that side-channel/injection attacks aim to read.
    The dealer is the only user with read permission on dealer.log —
    but see init_dealer_log() for the race-condition window, and
    send() for the command-injection path.
    """
    try:
        with open(DEALER_LOG, 'a') as f:
            f.write('=== Hand #%d ===\n' % hand_num)
            for player, cards in sorted(hands.items()):
                f.write('  Player %s: %s  %s\n' % (
                    player,
                    str(cards[0]) if cards else '?',
                    str(cards[1]) if len(cards) > 1 else '?'))
            f.write('\n')
    except OSError as e:
        print('[comm] WARNING: could not write dealer log: %s' % e,
              file=sys.stderr)
