# Python Poker — Project #2

Multiplayer Texas Hold'em. Each player is a separate process running as their own
Linux user (`player0` … `player15`). All communication goes through one library file,
`poker_communication.py`.

**Project #2 extension: an isolation mechanism.** The dealer is split into two
processes using privilege separation. The engine, which handles every untrusted
byte players send, is locked down in three ways, all enforced by the kernel:
- a **control group (cgroup)** that caps its memory, CPU and process count
- a separate, unprivileged user (`dealer`)
- a **seccomp-BPF** system-call filter

The library also isolates players from each other with per-connection quotas.

## Quick start

```bash
sudo bash poker_install.sh 4                      # create dealer + player0..player3
sudo python3 /opt/poker-dealer/dealer.py 4        # start the table (root: creates the cgroup)
sudo su - player0                                 # in another terminal, per player
python3 ~/poker_client.py
```

Uninstall: `sudo bash poker_uninstall.sh 4`

Dealer options:
- `--timeout=<sec>`: time to act (default 30)
- `--memory=<MB>`: engine memory limit (default 256)
- `--cpu=<percent>`: engine CPU limit, as a percent of one core (default 50)
- `--require-sandbox`: refuse to start without the cgroup and seccomp
- `--no-sandbox`: debugging only

Client commands: `FOLD`, `CHECK`, `CALL`, `RAISE:<n>` (or `raise 50`), `ALLIN`,
`say <text>` / `CHAT:<text>` (to everyone), `@<id> <text>` / `MSG:<id>:<text>`
(private message to one player), `WHO` (balances and who is connected), `HELP`.

## Files

| File | Installed to | Role |
|---|---|---|
| `poker_communication.py` | `/opt/poker/` | **The comms library.** Framing, authentication, routing, limits, logging |
| `poker_client.py` | `/opt/poker/` (symlinked into each player's home) | Player client: one receiver thread plus the input loop |
| `cards.py`, `banking.py` | `/opt/poker/` | Deck/rendering and balances (from Project #1) |
| `dealer.py` | `/opt/poker-dealer/` (root-only) | Game engine: betting, side pots, showdown |
| `sandbox.py` | `/opt/poker-dealer/` (root-only) | The isolation mechanism: supervisor, cgroup, seccomp |
| `poker_install.sh` / `poker_uninstall.sh` | — | Setup and teardown |

`makepoker_users.py` was removed. It was left over from the named-pipe version,
nothing used it, and it had a bug: line 31 ran `adduser` with a literal `%s` as
the username.

## Communication

The game uses one Unix domain stream socket, `/var/lib/poker/game.sock`. Each
message is a line of the form `:<sender>:<body>\n`. The dealer's `Server` class
runs a non-blocking `select()` loop that watches the listening socket and every
client, for both reading and writing.

| Pattern | Examples |
|---|---|
| dealer → one player (unicast) | `HOLE`, `YOUR_TURN`, `BALANCE`, error replies |
| dealer → all (broadcast) | `COMMUNITY`, `POT`, `PHASE`, `WINNER`, `TURN` |
| player → all (relayed broadcast) | `CHAT:<text>` |
| player → one player (relayed unicast) | `MSG:<id>:<text>`, delivered as `DM:` |
| player → dealer | `FOLD`, `CALL`, `RAISE:n`, `ALLIN`, `WHO` |

Identity comes only from `SO_PEERCRED`, which the kernel fills in. The server
ignores the sender field a client writes.

**Concurrency.** One event loop serves every client at once. While the dealer
waits for one player to act, everyone else can still chat, send private
messages, run `WHO`, disconnect or reconnect. Each client runs a receiver thread
alongside its input loop. The supervisor and engine are separate processes.

## The isolation mechanism (`sandbox.py`)

```
supervisor (root, unsandboxed)           engine (sandboxed)
  creates the engine's cgroup             joins its cgroup (while still root)
  creates game.sock (0660)   ──fork──>    drops root → dealer
  waits for the engine                    opens logs
  reports usage, removes                  not-dumpable, no_new_privs, seccomp
  the cgroup and the socket               then serves the players
```

The supervisor never reads player input. The engine applies these limits before
it accepts its first connection:

* **Control group (cgroup).** Both cgroup v2 (`/sys/fs/cgroup/poker/engine-<pid>/`,
  used by modern Ubuntu, Fedora and Debian) and the older v1 layout are supported.
  The kernel enforces these limits from outside the process:

  | Limit | v2 file | v1 file | Default | What it stops |
  |---|---|---|---|---|
  | Memory | `memory.max` (and `memory.swap.max` = 0) | `memory.limit_in_bytes` (and `memsw`) | 256 MB | A memory leak or attack: the kernel OOM-kills the engine, not the machine |
  | CPU | `cpu.max` | `cpu.cfs_quota_us` / `cfs_period_us` | 50% of one core | A runaway loop starving the computer |
  | Processes | `pids.max` | `pids.max` | 1 | The engine creating any process or thread |

  The cgroup files are root-owned, so after the engine drops to `dealer` it
  can't raise its own limits or leave the group. When the engine exits, the
  supervisor prints its peak memory, CPU time, throttling and any OOM kills,
  then deletes the cgroup.

  Why the dealer is started as root: creating a cgroup, and moving a process
  into it, needs root. Only the supervisor keeps root, and it never touches
  player input. To keep root from running code the `dealer` account could
  edit, `dealer.py` and `sandbox.py` are installed in a root-only directory.

* **seccomp-BPF filter**, built by hand with `ctypes` (no extra packages). The
  kernel returns `EPERM` for:
  * `execve`, `execveat` (no running programs)
  * `fork`, `vfork`, `clone`, `clone3` (no new processes)
  * `socket`, `connect`, `bind`, `listen` (no new network endpoints)
  * `ptrace`, `kill`, `process_vm_*` (no touching other processes)
  * `unlink`, `rename`, `mkdir`, `chmod`, `chown`, `link`, `truncate` (no
    filesystem changes)
  * `open`/`openat` with any write flag
  * `bpf`, `io_uring`, `userfaultfd`, `mount`, `unshare`, `setns` and other
    kernel attack surface

  The logs are opened before the filter goes up, so the engine keeps writing to
  them through file descriptors it already holds. The filter has syscall tables
  for x86_64 and aarch64, and it kills the process if a syscall arrives from a
  different ABI (x32, i386).
* **`PR_SET_DUMPABLE=0`.** No other process, not even one running as `dealer`,
  can ptrace the engine or read `/proc/<pid>/mem` to see hole cards. It also
  turns off core dumps, so a crash can't write the hole cards to disk.
* **Filesystem permissions.** Only the `pokerplayers` group can reach the
  socket. Players can't read `/home/dealer` (mode 0700). Logs are created 0600
  atomically with `O_NOFOLLOW`.

So even if a player found a code-execution bug in the engine (Project #1 had
one: see the chat item below), the kernel would block it from starting a shell,
connecting anywhere or damaging files.

**Isolation between players** (in `poker_communication.py`):

| Limit | Value | What happens |
|---|---|---|
| Max line length | 512 bytes | Connection dropped |
| Partial line without newline | 512 bytes | Connection dropped. Each connection can hold at most about 4.6 KB of input |
| Output queue for a client that stops reading | 64 KB | Connection dropped, so it can't stall the dealer |
| All messages | 10/s, burst 30 | Connection dropped for flooding |
| Chat and private messages | 1 per 2 s, burst 5 | Error reply |
| Time to authenticate | 10 s, at most 8 pending connections | Connection dropped |
| Chat text | 200 printable characters | Control characters and ANSI escapes removed |

## Peer-review issues and other fixes

| Issue | Cause | Fix |
|---|---|---|
| **Typing without Enter grows server memory** (peer) | `recv_from` added to the buffer with no limit | Lines over 512 bytes, or partial lines past 512 bytes, drop the connection |
| **A player quitting mid-round ends the round for everyone** (peer) | An empty `select()` result, whether from a disconnect, a timeout or even a partial line, auto-folded *every* player still to act. It could be exploited: raise, then send half a line, and everyone else folds. | Disconnects are now explicit `leave` events. Only that player folds. An all-in player who disconnects keeps their share of the pot and still shows down. A timeout folds only the player whose turn it is (it checks if they owe nothing). Players can reconnect and rejoin on the next hand. |
| **Balance goes negative from blinds** (peer) | Blinds were taken without checking the balance | Every chip movement goes through `Hand.bet()`, which caps at the balance and marks the player all-in. Real side pots are built from each player's total contribution. |
| **Command injection through chat** ("vulnerabilities enabled by default") | `log_chat` ran `os.popen('echo "...%s" >> log')`, so `CHAT:"; rm -rf ~ #` ran as dealer | Logs are written through an already-open file. seccomp also blocks `execve` as a second layer. |
| Socket and socket directory were 0777 | `create_server_socket` | Socket is 0660 and only the `pokerplayers` group can reach it; it is created with a umask, so it is never briefly open |
| Players were in the `dealer` group | `poker_install.sh` | They are only in `pokerplayers` now, and the installer removes the old membership |
| Dealer log briefly world-readable | `touch` followed by `chmod` | Created atomically with mode 0600 and `O_NOFOLLOW` |
| One player could take over another's terminal | Chat text passed through raw | Text is cleaned on the server, and again on the client |
| Partial frames when a client's buffer was full | `sendall` on a non-blocking socket | Per-connection output queues are flushed when `select` reports the socket writable |
| Wrong hand comparisons | Tiebreak sorted by rank only (pair of 2s with an ace beat a pair of kings) | Ranks are grouped by count first; the wheel straight is ranked as 5-high |
| Ties gave the whole pot to one player | — | Pots are split, and the odd chip goes to the first player left of the button |
| Blinds always posted by the same two seats; no turn order | — | Button rotates; standard heads-up and multi-way action order; minimum raise; `CHECK` and `ALLIN` commands |

## Testing done

The tests ran on Linux x86_64 (kernel 6.18, cgroup v1 layout), Python 3.13,
with real `playerN` accounts:

* cgroup limits enforced by the kernel:
  * 200 MB allocated under a 64 MB cap was OOM-killed, with `oom_kill` = 1.
  * A 2-second busy loop at a 20% cap got 0.42 s of CPU.
  * `fork()` with `pids.max=1` failed with `EAGAIN`.
  * The `dealer` user got `Permission denied` trying to raise the memory limit
    or move the engine out of its cgroup.
  * The cgroup was removed after normal exit, Ctrl-C and SIGTERM.
  * The cgroup v2 code path was checked against a mock v2 directory tree. This
    machine's controllers are bound to v1, so v2 limits weren't enforced live;
    on your VM, the dealer's first log lines say which version it used.
  * The cgroup also caught a real problem: `ctypes.util.find_library()` silently
    starts helper programs, and `pids.max` refused them. The code now loads
    libc directly.

* A full game with bots covering all-in, calling and raising. Side pots were
  correct and the dealer shut down cleanly.
* Attack bots:
  * The chat injection did not create its file, and its ANSI codes were removed.
  * A 100 KB message with no newline got the connection dropped at 512 bytes,
    and the player then reconnected.
  * A player quitting mid-hand folded only that player.
  * A player who never acted was timed out without affecting anyone else.
  * A private message reached only its target.
  * A player spamming messages was disconnected for flooding.
* Scripted game-state tests: a short-stack blind (balance 5 posts a 5-chip
  all-in, nobody goes negative, chips are conserved), a disconnect mid-hand,
  a timeout, an out-of-turn action being rejected, and an all-in player who
  disconnects still showing down.
* Access checks:
  * A non-player account gets `Permission denied` on the socket.
  * `player0` sending `READY:1` gets auth failed.
  * Players can't read `/home/dealer`.
  * The engine shows `Seccomp: 2`.
  * Even the `dealer` user can't read the engine's memory.
* In the sandboxed engine, `execve`, `fork`, `socket`, `unlink`, `mkdir`, `kill`
  and opening files for writing all return `EPERM`. Writing to the logs it
  opened earlier and reading files still work.
