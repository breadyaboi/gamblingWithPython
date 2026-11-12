"""
sandbox.py — the isolation mechanism added in Project #2
=========================================================
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

import ctypes, os, platform, struct, sys, signal, time

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


def enter_sandbox(require=False):
    """
    Lock down the current process (call after joining the cgroup and dropping
    privileges).  Returns a list of what was applied.
    If require=True, raise if the seccomp filter can't be installed.
    """
    applied = []
    libc = _libc()
    try:
        _prctl(libc, PR_SET_DUMPABLE, 0)
        applied.append('not-dumpable')
    except OSError:
        pass

    arch = platform.machine()
    if arch not in _DENY:
        msg = 'seccomp: no syscall table for %s' % arch
        if require:
            raise RuntimeError(msg)
        print('[sandbox] WARNING: ' + msg, file=sys.stderr)
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
