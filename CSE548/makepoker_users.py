#!/usr/bin/env python3
"""
makepoker_users.py
==================
Creates playerN Unix accounts (0..N-1) and the named pipes under
/home/dealer/pipes/ that the poker game uses.

Usage:
    python3 makepoker_users.py <num_players>
    
Permission logic (mirrors Virtual Werewolves mkusr.sh):
  <N>todD/         chmod 350   owner=player(wx)  group=dealer(r-x)
  <N>todD/<N>tod   chmod 660   player rw, dealer rw
  dto<N>D/         chmod 530   owner=dealer(r-x) group=player(wx)
  dto<N>D/dto<N>   chmod 640   dealer rw, player r
"""

import subprocess, sys, os

def run(cmd, check=True):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        print('[makepoker] WARNING: %s\n  stderr: %s' % (cmd, result.stderr.strip()))
    return result

def make_player(n, pipe_root='/home/dealer/pipes'):
    username = 'player%d' % n
    print('[makepoker] Setting up %s ...' % username)

    # Create user account (skip if exists)
    run('sudo adduser --disabled-password --gecos "" %s 2>/dev/null || true',
        check=False)
    run('sudo adduser --disabled-password --gecos "" %s' % username, check=False)

    # ── Player → Dealer ───────────────────────────────────────────────────
    p2d_dir  = '%s/%stodD'  % (pipe_root, n)
    p2d_pipe = '%s/%stod'   % (p2d_dir,   n)

    run('sudo mkdir -p %s' % p2d_dir)
    run('sudo mkfifo %s 2>/dev/null || true' % p2d_pipe, check=False)

    # Directory: player owns it, dealer group can read/exec
    run('sudo chown %s  %s' % (username, p2d_dir))
    run('sudo chgrp dealer %s' % p2d_dir)
    run('sudo chmod 350  %s' % p2d_dir)    # owner=wx(3) group=r-x(5) other=---(0)

    # FIFO: both player and dealer need rw
    run('sudo chown %s  %s' % (username, p2d_pipe))
    run('sudo chgrp dealer %s' % p2d_pipe)
    run('sudo chmod 660  %s' % p2d_pipe)   # player rw, dealer rw

    # ── Dealer → Player ───────────────────────────────────────────────────
    d2p_dir  = '%s/dto%sD'  % (pipe_root, n)
    d2p_pipe = '%s/dto%s'   % (d2p_dir,   n)

    run('sudo mkdir -p %s' % d2p_dir)
    run('sudo mkfifo %s 2>/dev/null || true' % d2p_pipe, check=False)

    # Directory: dealer owns it, player group can write/exec
    run('sudo chown dealer %s' % d2p_dir)
    run('sudo chgrp %s  %s' % (username, d2p_dir))
    run('sudo chmod 530  %s' % d2p_dir)    # dealer=r-x(5) player=wx(3)

    # FIFO: dealer writes, player reads
    run('sudo chown dealer %s' % d2p_pipe)
    run('sudo chgrp %s  %s' % (username, d2p_pipe))
    run('sudo chmod 640  %s' % d2p_pipe)   # dealer rw(6), player r(4)

    # Copy client script to player home
    for f in ('poker_client.py', 'cards.py', 'banking.py'):
        src = '/home/dealer/%s' % f
        dst = '/home/%s/%s' % (username, f)
        run('sudo cp %s %s 2>/dev/null || true' % (src, dst), check=False)
        run('sudo chown %s %s 2>/dev/null || true' % (username, dst), check=False)
        run('sudo chmod 750 %s 2>/dev/null || true' % dst, check=False)

    print('[makepoker] Player %d ready.' % n)


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    for i in range(n):
        make_player(i)
    print('\n[makepoker] Done — %d players created.' % n)
    print('[makepoker] Pipes under /home/dealer/pipes/')
