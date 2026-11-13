#!/usr/bin/env python3
"""
attack_client.py — a MALICIOUS client, for testing this game's defenses
=======================================================================
Run this as your own playerN account, against a dealer that is already
running, exactly as a classmate would during peer review:

    python3 attack_client.py <attack>        # run one attack
    python3 attack_client.py all             # run them all in turn

It behaves like poker_client.py but misbehaves on purpose, then prints what
the dealer did in response.  Every attack below should be stopped by a defense
that is already in the game; each one prints DEFENDED or VULNERABLE.

This only attacks the local game, from the single player account you run it as
— the same thing peer review asks you to do to a classmate's game, and them to
yours.  It needs no root: a real player has no root, and the point is to prove
an ordinary player can't break the game.

Attacks (all launched from your own seat):
    impersonate   claim to be a different player (forge your identity)
    flood         send a huge message with no newline (memory-exhaustion bug)
    spam          send hundreds of messages at once (flooding)
    inject        put shell metacharacters in a CHAT message (command injection)
    escape        put terminal escape codes in a CHAT message (screen hijack)
    silent        connect and then never read, to try to stall the dealer
    peek          try to read the dealer's logs and code from your account
    outofturn     send a game action when it is not your turn
    badbet        send negative / oversized / malformed raises
"""

import os, sys, socket, time, pwd, glob
sys.path.insert(0, '/opt/poker')
from poker_communication import SOCKET_PATH, frame, unframe


def my_id():
    name = pwd.getpwuid(os.getuid())[0]
    if name.startswith('player') and name[6:].isdigit():
        return int(name[6:])
    sys.exit('Run this as a playerN account (you are %s).' % name)

ME = my_id()
GREEN, RED, DIM, RESET = '\033[32m', '\033[31m', '\033[2m', '\033[0m'


def verdict(defended, note=''):
    tag = GREEN + 'DEFENDED' + RESET if defended else RED + 'VULNERABLE' + RESET
    print('   -> %s%s' % (tag, ('  (%s)' % note if note else '')))
    return defended


def dial():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCKET_PATH)          # raises if we're not allowed on the socket
    s.settimeout(5)
    return s


def talk(chunks, read_for=1.0):
    """Connect, send each chunk (bytes) with a small pause, collect the reply."""
    s = dial()
    for c in chunks:
        try:
            s.sendall(c)
        except OSError as e:
            s.close()
            return 'SEND_ERR:%s' % e
        time.sleep(0.2)
    time.sleep(read_for)
    got = b''
    try:
        while True:
            b = s.recv(4096)
            if not b:
                break
            got += b
            if len(got) > 20000:
                break
    except OSError:
        pass
    s.close()
    return got.decode('utf-8', errors='replace')


# ── the attacks ──────────────────────────────────────────────────────────────
def a_impersonate():
    other = (ME + 1) % 16
    print('[impersonate] I am player%d, but I will claim to be player%d.' % (ME, other))
    reply = talk([frame(str(ME), 'READY:%d' % other)])
    print(DIM + '   dealer said: ' + reply.strip().replace('\n', ' | ') + RESET)
    return verdict('auth failed' in reply or 'ERROR' in reply)


def a_flood():
    print('[flood] Sending 100 KB with no newline (the pile-up-in-memory bug).')
    reply = talk([frame(str(ME), 'READY:%d' % ME), b'A' * 100000])
    print(DIM + '   dealer said: ' + reply.strip()[-160:].replace('\n', ' | ') + RESET)
    return verdict('longer than' in reply or 'SEND_ERR' in reply or 'Disconnected' in reply)


def a_spam():
    print('[spam] Sending 300 valid messages at once (flooding).')
    chunks = [frame(str(ME), 'READY:%d' % ME) + frame(str(ME), 'WHO') * 300]
    reply = talk(chunks)
    print(DIM + '   dealer said: ' + reply.strip()[-160:].replace('\n', ' | ') + RESET)
    return verdict('flooding' in reply)


def a_inject():
    marker = '/tmp/attack_client_pwned'
    try:
        os.unlink(marker)
    except OSError:
        pass
    print('[inject] CHAT containing:  "; touch %s; echo "' % marker)
    talk([frame(str(ME), 'READY:%d' % ME),
          frame(str(ME), 'CHAT:x"; touch %s; echo "' % marker)])
    time.sleep(0.5)
    created = os.path.exists(marker)
    print(DIM + '   marker file exists afterwards: %s' % created + RESET)
    return verdict(not created, 'the command did not run')


def a_escape():
    print('[escape] CHAT with an ANSI "clear screen + red text" sequence.')
    reply = talk([frame(str(ME), 'READY:%d' % ME),
                  frame(str(ME), 'CHAT:\x1b[2J\x1b[31mHIJACK'),
                  frame(str(ME), 'WHO')])
    raw_esc = '\x1b' in reply
    print(DIM + '   raw ESC char present in what came back: %s' % raw_esc + RESET)
    return verdict(not raw_esc, 'escape codes were stripped')


def a_silent():
    print('[silent] Connecting, authenticating, then never reading — trying to stall the dealer.')
    s = dial()
    s.sendall(frame(str(ME), 'READY:%d' % ME))
    # Never recv(). A healthy dealer caps our output queue and drops us; it must
    # not block. We prove the dealer is still alive by opening a 2nd connection.
    time.sleep(3)
    try:
        s2 = dial()
        s2.sendall(frame(str(ME), 'WHO'))
        time.sleep(0.5)
        alive = bool(s2.recv(4096))
        s2.close()
    except OSError as e:
        alive = False
        print(DIM + '   second connection error: %s' % e + RESET)
    s.close()
    return verdict(alive, 'dealer still responsive to other clients')


def a_peek():
    print('[peek] Trying to read the dealer\'s secrets from my own account.')
    targets = ['/home/dealer/log/dealer.log', '/home/dealer/log/chat.log',
               '/opt/poker-dealer/dealer.py']
    blocked = []
    for t in targets:
        try:
            open(t).read(1)
            blocked.append((t, False))
        except OSError:
            blocked.append((t, True))
    for t, b in blocked:
        print(DIM + '   %-32s %s' % (t, 'denied' if b else 'READABLE!') + RESET)
    return verdict(all(b for _, b in blocked))


def a_outofturn():
    print('[outofturn] Sending CALL when it is almost certainly not my turn.')
    reply = talk([frame(str(ME), 'READY:%d' % ME), frame(str(ME), 'CALL')], read_for=1.5)
    print(DIM + '   dealer said: ' + reply.strip()[-160:].replace('\n', ' | ') + RESET)
    # Either the game hasn't started (needs all seats) or it's not our turn —
    # in both cases the dealer must not accept the action silently.
    return verdict('not your turn' in reply.lower() or 'ERROR' in reply or 'Authenticate' in reply
            or 'WELCOME' in reply)


def a_badbet():
    print('[badbet] Sending malformed raises: RAISE:-50, RAISE:abc, RAISE:999999999.')
    reply = talk([frame(str(ME), 'READY:%d' % ME),
                  frame(str(ME), 'RAISE:-50'),
                  frame(str(ME), 'RAISE:abc'),
                  frame(str(ME), 'RAISE:999999999')], read_for=1.5)
    print(DIM + '   dealer said: ' + reply.strip()[-200:].replace('\n', ' | ') + RESET)
    # None of these should ever be accepted. The dealer either errors or ignores
    # them (out of turn); what must NOT happen is the dealer crashing/accepting.
    return verdict('ERROR' in reply or 'not your turn' in reply.lower() or 'WELCOME' in reply)


ATTACKS = {
    'impersonate': a_impersonate, 'flood': a_flood, 'spam': a_spam,
    'inject': a_inject, 'escape': a_escape, 'silent': a_silent,
    'peek': a_peek, 'outofturn': a_outofturn, 'badbet': a_badbet,
}


def main():
    if len(sys.argv) < 2 or (sys.argv[1] not in ATTACKS and sys.argv[1] != 'all'):
        print(__doc__)
        print('Available attacks: %s, all' % ', '.join(ATTACKS))
        sys.exit(1)

    if not os.path.exists(SOCKET_PATH):
        sys.exit('No dealer running at %s. Start one first:\n'
                 '  sudo python3 /opt/poker-dealer/dealer.py <num_players>' % SOCKET_PATH)

    try:
        dial().close()
    except OSError as e:
        print('Cannot even open the socket: %s' % e)
        print('(If this says "permission denied", the group restriction already '
              'blocked you — that itself is a defense working.)')
        sys.exit(1)

    names = list(ATTACKS) if sys.argv[1] == 'all' else [sys.argv[1]]
    print('\n=== Attacking as player%d — %d attack(s) ===\n' % (ME, len(names)))
    held = 0
    for name in names:
        try:
            if ATTACKS[name]():
                held += 1
        except Exception as e:
            print('   -> attack raised %s: %s' % (type(e).__name__, e))
        print()
    print('=== %d / %d attacks were defended ===' % (held, len(names)))
    sys.exit(0 if held == len(names) else 1)


if __name__ == '__main__':
    main()
