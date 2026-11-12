"""
sandbox.py — the isolation mechanism added in Project #2
=========================================================
The dealer runs as two processes (privilege separation, like OpenSSH):

  supervisor   unsandboxed.  It creates the listening socket, forks the engine,
               waits for it, and afterwards removes the socket file.  It never
               reads a byte a player sends.

  engine       runs the whole game and talks to every player.  This is the
               process that parses untrusted input, so before it accepts a
               single connection it locks itself down:

    1. Privilege drop    If started as root, switch to the `dealer` user.
    2. Resource limits   RLIMIT_NOFILE caps sockets (no fd exhaustion),
                         RLIMIT_AS caps memory, RLIMIT_FSIZE caps log size,
                         RLIMIT_CORE=0 means a crash can't dump the hole cards.
    3. Not dumpable      PR_SET_DUMPABLE=0: no other process, even one running
                         as `dealer`, can ptrace it or read /proc/<pid>/mem to
                         peek at players' hole cards.
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

import ctypes, ctypes.util, os, platform, resource, struct, sys, signal

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
    libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
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


def apply_rlimits(max_fds):
    def lim(r, v):
        try:
            resource.setrlimit(r, (v, v))
        except (ValueError, OSError):
            pass
    lim(resource.RLIMIT_NOFILE, max_fds)
    lim(resource.RLIMIT_CORE, 0)
    lim(resource.RLIMIT_FSIZE, 64 * 1024 * 1024)
    lim(resource.RLIMIT_AS, 1024 * 1024 * 1024)


def enter_sandbox(max_fds=64, require=False):
    """
    Lock down the current process.  Returns a list of what was applied.
    If require=True, raise if the seccomp filter can't be installed.
    """
    applied = []
    apply_rlimits(max_fds)
    applied.append('rlimits(nofile=%d, core=0, fsize=64M, as=1G)' % max_fds)

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
