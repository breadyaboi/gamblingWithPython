"""
sandbox.py
==========
Isolation primitives for the poker dealer (Project #2).

Everything here uses unprivileged Linux kernel features, so the dealer can
run as the ordinary `dealer` account (no root, no setuid helpers):

  * prctl(PR_SET_NO_NEW_PRIVS)  - the process can never gain privileges
                                  again (required before seccomp/Landlock)
  * prctl(PR_SET_DUMPABLE, 0)   - other processes with the same UID cannot
                                  ptrace us or read /proc/<pid>/mem, and no
                                  core file is written; protects hole cards
                                  held in the dealer's memory
  * setrlimit()                 - caps memory, CPU, file size, open files and
                                  process count
  * seccomp-BPF                 - a system-call filter the kernel enforces
  * Landlock                    - a file-system allow-list (kernel >= 5.13)

Two profiles are provided:

  harden_dealer()               applied to the dealer itself after start-up
  confine_scribe(chat_log)      applied to the chat-log helper process, the
                                one place where untrusted chat text meets a
                                shell (see poker_communication.ChatScribe)

Each function returns a list of human-readable status strings.  A feature the
running kernel doesn't support is reported as "unavailable" rather than
crashing the game.
"""

import ctypes, ctypes.util, errno, os, platform, resource, signal, struct

_libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.prctl.restype = ctypes.c_int

PR_SET_DUMPABLE      = 4
PR_SET_SECCOMP       = 22
PR_SET_NO_NEW_PRIVS  = 38
SECCOMP_MODE_FILTER  = 2

AF_UNIX = 1


# ── Architecture tables ──────────────────────────────────────────────────────
# Syscall numbers differ by CPU architecture, so the filter has to be built
# for the machine it runs on.  None = syscall doesn't exist on that arch.
_ARCH = platform.machine()

_AUDIT_ARCH = {'x86_64': 0xC000003E, 'aarch64': 0xC00000B7}.get(_ARCH)

_NR = {
    'x86_64': {
        'socket': 41, 'socketpair': 53, 'connect': 42, 'bind': 49,
        'listen': 50, 'kill': 62, 'tkill': 200, 'tgkill': 234,
        'ptrace': 101, 'process_vm_readv': 310, 'process_vm_writev': 311,
        'mount': 165, 'umount2': 166, 'pivot_root': 155, 'chroot': 161,
        'unshare': 272, 'setns': 308, 'bpf': 321, 'perf_event_open': 298,
        'keyctl': 250, 'add_key': 248, 'request_key': 249,
        'init_module': 175, 'finit_module': 313, 'delete_module': 176,
        'kexec_load': 246, 'reboot': 169, 'swapon': 167, 'swapoff': 168,
        'userfaultfd': 323, 'io_uring_setup': 425,
        'pidfd_open': 434, 'pidfd_send_signal': 424, 'pidfd_getfd': 438,
        'rt_sigqueueinfo': 129, 'rt_tgsigqueueinfo': 297,
        'memfd_create': 319,
    },
    'aarch64': {
        'socket': 198, 'socketpair': 199, 'connect': 203, 'bind': 200,
        'listen': 201, 'kill': 129, 'tkill': 130, 'tgkill': 131,
        'ptrace': 117, 'process_vm_readv': 270, 'process_vm_writev': 271,
        'mount': 40, 'umount2': 39, 'pivot_root': 41, 'chroot': 51,
        'unshare': 97, 'setns': 268, 'bpf': 280, 'perf_event_open': 241,
        'keyctl': 219, 'add_key': 217, 'request_key': 218,
        'init_module': 105, 'finit_module': 273, 'delete_module': 106,
        'kexec_load': 104, 'reboot': 142, 'swapon': 224, 'swapoff': 225,
        'userfaultfd': 282, 'io_uring_setup': 425,
        'pidfd_open': 434, 'pidfd_send_signal': 424, 'pidfd_getfd': 438,
        'rt_sigqueueinfo': 138, 'rt_tgsigqueueinfo': 240,
        'memfd_create': 279,
    },
}.get(_ARCH)

# Newer syscalls share one number across architectures
_NR_LANDLOCK_CREATE_RULESET = 444
_NR_LANDLOCK_ADD_RULE       = 445
_NR_LANDLOCK_RESTRICT_SELF  = 446


def _prctl(option, arg2=0, arg3=0, arg4=0, arg5=0):
    if _libc.prctl(option, ctypes.c_ulong(arg2), ctypes.c_ulong(arg3),
                   ctypes.c_ulong(arg4), ctypes.c_ulong(arg5)) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


# ── Resource limits ───────────────────────────────────────────────────────────
def _set_limit(name, soft, hard=None):
    res = getattr(resource, name)
    cur_soft, cur_hard = resource.getrlimit(res)
    hard = soft if hard is None else hard
    # We can only lower the hard limit, never raise it
    if cur_hard != resource.RLIM_INFINITY:
        hard = min(hard, cur_hard)
        soft = min(soft, hard)
    resource.setrlimit(res, (soft, hard))


# ── seccomp-BPF ───────────────────────────────────────────────────────────────
# Classic BPF opcodes used below
_BPF_LD_W_ABS = 0x20          # BPF_LD | BPF_W | BPF_ABS
_BPF_JEQ_K    = 0x15          # BPF_JMP | BPF_JEQ | BPF_K
_BPF_JGE_K    = 0x35          # BPF_JMP | BPF_JGE | BPF_K
_BPF_RET_K    = 0x06          # BPF_RET | BPF_K

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO        = 0x00050000
SECCOMP_RET_ALLOW        = 0x7FFF0000

# offsets inside struct seccomp_data
_OFF_NR, _OFF_ARCH, _OFF_ARG0 = 0, 4, 16


class _SockFilter(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]

class _SockFprog(ctypes.Structure):
    _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(_SockFilter))]


def _install_seccomp(deny, unix_sockets_only=False):
    """
    Install a deny-list filter.  Every syscall named in `deny` fails with
    EPERM.  If unix_sockets_only, socket() is allowed only for AF_UNIX (no
    TCP/UDP, so nothing can phone home over the network).
    A syscall from a foreign ABI (e.g. 32-bit int 0x80 on x86_64) kills the
    process, closing the classic seccomp bypass.
    """
    if _AUDIT_ARCH is None or _NR is None:
        raise OSError(errno.ENOSYS, 'unsupported architecture %s' % _ARCH)

    eperm = SECCOMP_RET_ERRNO | errno.EPERM
    deny_nrs = sorted({_NR[n] for n in deny if n in _NR})

    prog = []
    def ins(code, k, jt=0, jf=0):
        prog.append((code, jt, jf, k))

    ins(_BPF_LD_W_ABS, _OFF_ARCH)
    ins(_BPF_JEQ_K, _AUDIT_ARCH, 1, 0)
    ins(_BPF_RET_K, SECCOMP_RET_KILL_PROCESS)
    ins(_BPF_LD_W_ABS, _OFF_NR)
    if _ARCH == 'x86_64':
        # x32 ABI syscalls carry bit 30; refuse them all
        ins(_BPF_JGE_K, 0x40000000, 0, 1)
        ins(_BPF_RET_K, eperm)
    for nr in deny_nrs:
        ins(_BPF_JEQ_K, nr, 0, 1)
        ins(_BPF_RET_K, eperm)
    if unix_sockets_only and 'socket' not in deny:
        ins(_BPF_JEQ_K, _NR['socket'], 0, 3)
        ins(_BPF_LD_W_ABS, _OFF_ARG0)                 # domain argument
        ins(_BPF_JEQ_K, AF_UNIX, 1, 0)
        ins(_BPF_RET_K, SECCOMP_RET_ERRNO | errno.EAFNOSUPPORT)
    ins(_BPF_RET_K, SECCOMP_RET_ALLOW)

    arr = (_SockFilter * len(prog))(*[_SockFilter(c, jt, jf, k)
                                      for c, jt, jf, k in prog])
    fprog = _SockFprog(len(prog), arr)
    _prctl(PR_SET_NO_NEW_PRIVS, 1)
    if _libc.prctl(PR_SET_SECCOMP, ctypes.c_ulong(SECCOMP_MODE_FILTER),
                   ctypes.byref(fprog), 0, 0) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return len(deny_nrs)


# ── Landlock ──────────────────────────────────────────────────────────────────
_LL_EXECUTE     = 1 << 0
_LL_WRITE_FILE  = 1 << 1
_LL_READ_FILE   = 1 << 2
_LL_READ_DIR    = 1 << 3
_LL_TRUNCATE    = 1 << 14
_LL_FILE_ONLY   = _LL_EXECUTE | _LL_WRITE_FILE | _LL_READ_FILE | _LL_TRUNCATE

def _ll_handled(abi):
    """All file-system rights the given Landlock ABI knows about."""
    mask = (1 << 13) - 1                   # ABI 1: bits 0..12
    if abi >= 2: mask |= 1 << 13           # REFER
    if abi >= 3: mask |= 1 << 14           # TRUNCATE
    if abi >= 5: mask |= 1 << 15           # IOCTL_DEV
    return mask

class _RulesetAttr(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]

class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def landlock_abi():
    v = _libc.syscall(_NR_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                      ctypes.c_uint32(1))     # LANDLOCK_CREATE_RULESET_VERSION
    return v if v > 0 else 0


def _install_landlock(rules):
    """
    rules: [(path, access_bits), ...].  After this call the process (and every
    child it ever creates) can touch only these paths, with only these rights.
    Missing paths are skipped.
    """
    abi = landlock_abi()
    if abi == 0:
        raise OSError(errno.ENOSYS, 'Landlock not enabled in this kernel')
    handled = _ll_handled(abi)
    attr = _RulesetAttr(handled)
    rfd = _libc.syscall(_NR_LANDLOCK_CREATE_RULESET, ctypes.byref(attr),
                        ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if rfd < 0:
        e = ctypes.get_errno(); raise OSError(e, os.strerror(e))
    try:
        for path, access in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                if not os.path.isdir(path):
                    access &= _LL_FILE_ONLY      # dir-only rights invalid on files
                pb = _PathBeneathAttr(access & handled, fd)
                if _libc.syscall(_NR_LANDLOCK_ADD_RULE, ctypes.c_int(rfd),
                                 ctypes.c_int(1),            # PATH_BENEATH
                                 ctypes.byref(pb), ctypes.c_uint32(0)) != 0:
                    e = ctypes.get_errno(); raise OSError(e, '%s: %s' % (path, os.strerror(e)))
            finally:
                os.close(fd)
        _prctl(PR_SET_NO_NEW_PRIVS, 1)
        if _libc.syscall(_NR_LANDLOCK_RESTRICT_SELF, ctypes.c_int(rfd),
                         ctypes.c_uint32(0)) != 0:
            e = ctypes.get_errno(); raise OSError(e, os.strerror(e))
    finally:
        os.close(rfd)
    return abi


def _attempt(status, label, fn, *args):
    try:
        r = fn(*args)
        status.append('%s: on%s' % (label, '' if r is None else ' (%s)' % r))
        return True
    except (OSError, ValueError) as e:
        status.append('%s: unavailable (%s)' % (label, e))
        return False


# ── Dealer profile ────────────────────────────────────────────────────────────
DEALER_DENY = [
    'ptrace', 'process_vm_readv', 'process_vm_writev', 'pidfd_getfd',
    'mount', 'umount2', 'pivot_root', 'chroot', 'unshare', 'setns',
    'bpf', 'perf_event_open', 'keyctl', 'add_key', 'request_key',
    'init_module', 'finit_module', 'delete_module', 'kexec_load',
    'reboot', 'swapon', 'swapoff', 'userfaultfd', 'io_uring_setup',
]

def harden_dealer(max_open_files=256):
    """
    Lock down the dealer process.  Call after the server socket is bound and
    before any player connects.
    """
    status = []
    def limits():
        _set_limit('RLIMIT_CORE', 0)                   # no core dumps of hole cards
        _set_limit('RLIMIT_NOFILE', max_open_files)    # bounds connection flood
        return 'core=0 nofile=%d' % max_open_files
    _attempt(status, 'rlimits', limits)
    _attempt(status, 'no_new_privs', _prctl, PR_SET_NO_NEW_PRIVS, 1)
    _attempt(status, 'non-dumpable', _prctl, PR_SET_DUMPABLE, 0)
    _attempt(status, 'seccomp',
             lambda: '%d syscalls denied, AF_UNIX sockets only' %
                     _install_seccomp(DEALER_DENY, unix_sockets_only=True))
    return status


# ── Scribe (chat logger) profile ──────────────────────────────────────────────
SCRIBE_DENY = DEALER_DENY + [
    # no new sockets at all: can't connect to game.sock and pose as a player
    'socket', 'socketpair', 'connect', 'bind', 'listen',
    # can't signal the dealer or anything else
    'kill', 'tkill', 'tgkill', 'pidfd_open', 'pidfd_send_signal',
    'rt_sigqueueinfo', 'rt_tgsigqueueinfo',
    'memfd_create',
]

SCRIBE_LIMITS = {
    'RLIMIT_AS':     512 * 1024 * 1024,   # memory
    'RLIMIT_CPU':    30,                  # seconds of CPU, then SIGXCPU
    'RLIMIT_FSIZE':  5 * 1024 * 1024,     # chat.log can't fill the disk
    'RLIMIT_NOFILE': 32,
    'RLIMIT_NPROC':  24,                  # no fork bombs (counts all dealer-UID procs)
    'RLIMIT_CORE':   0,
}

def confine_scribe(chat_log_path, protected_dirs=()):
    """
    Confine the chat-log helper.  It ends up able to:
      - read and execute system binaries and libraries (it runs /bin/sh)
      - append to chat.log, read/write /dev/null
    and nothing else: no dealer.log (hole cards), no game socket, no network,
    no signals, no ptrace, bounded CPU/memory/disk/processes.
    """
    status = []

    def limits():
        for name, val in SCRIBE_LIMITS.items():
            _set_limit(name, val)
        # Over-sized writes fail with EFBIG instead of killing the helper
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        return ', '.join('%s=%s' % (k[7:].lower(), v) for k, v in SCRIBE_LIMITS.items())
    _attempt(status, 'rlimits', limits)
    _attempt(status, 'no_new_privs', _prctl, PR_SET_NO_NEW_PRIVS, 1)

    rx = _LL_READ_FILE | _LL_READ_DIR | _LL_EXECUTE
    rules = [(p, rx) for p in ('/usr', '/bin', '/sbin', '/lib', '/lib32',
                               '/lib64', '/etc')]
    # Python's own install, unless it lives inside a protected directory
    import sys
    for p in {sys.prefix, sys.base_prefix, sys.exec_prefix}:
        if not any(os.path.realpath(p).startswith(os.path.realpath(d))
                   for d in protected_dirs):
            rules.append((p, rx))
    rules.append(('/dev/null', _LL_READ_FILE | _LL_WRITE_FILE))
    rules.append((chat_log_path, _LL_WRITE_FILE))
    _attempt(status, 'landlock', lambda: 'ABI %d, %d paths' %
             (_install_landlock(rules), len(rules)))

    _attempt(status, 'seccomp', lambda: '%d syscalls denied' %
             _install_seccomp(SCRIBE_DENY))
    return status
