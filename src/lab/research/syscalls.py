"""Linux syscall boundary for generated-code workers, installed before compiling source.

File-descriptor and file-size limits do not block unlink/rename, and closing stderr frees
a descriptor for open/socket. Default-deny seccomp makes those operations unavailable even
if a strategy reaches ctypes or another native library. No optional insecure fallback.

API: https://man7.org/linux/man-pages/man3/seccomp_rule_add.3.html
Only the child calls install(); the parent and broker process are never filtered.
"""

import ctypes
import errno
import sys

# Imported by the trusted worker before descriptor limits are applied.
_LIB = ctypes.CDLL("libseccomp.so.2", use_errno=True) if sys.platform == "linux" else None


def install() -> None:
    if sys.platform != "linux":
        raise RuntimeError("generated signals require Linux with libseccomp")
    lib = _LIB
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None
    ctx = lib.seccomp_init(0x00050000 | errno.EPERM)  # SCMP_ACT_ERRNO(EPERM)
    if not ctx:
        raise RuntimeError("cannot allocate syscall filter")
    # No path access, sockets, process creation, signalling other processes, ptrace,
    # ioctl, io_uring, fd duplication or privilege changes. Existing fds are only IPC.
    allowed = (
        "read",
        "write",
        "close",
        "fstat",
        "lseek",
        "poll",
        "ppoll",
        "select",
        "pselect6",
        "brk",
        "mmap",
        "mmap2",
        "munmap",
        "mremap",
        "mprotect",
        "madvise",
        "futex",
        "futex_time64",
        "clock_gettime",
        "clock_gettime64",
        "gettimeofday",
        "time",
        "nanosleep",
        "clock_nanosleep",
        "rt_sigaction",
        "rt_sigprocmask",
        "rt_sigreturn",
        "sigaltstack",
        "restart_syscall",
        "getpid",
        "gettid",
        "getuid",
        "geteuid",
        "getgid",
        "getegid",
        "sched_yield",
        "getrandom",
        "getrusage",
        "exit",
        "exit_group",
    )
    try:
        for name in allowed:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number == -1:  # syscall absent on this architecture
                continue
            code = lib.seccomp_rule_add(ctx, 0x7FFF0000, number, 0)  # SCMP_ACT_ALLOW
            if code < 0:
                raise RuntimeError(f"cannot allow {name}: {code}")
        code = lib.seccomp_load(ctx)  # libseccomp enables no_new_privs; cannot later remove the filter
        if code < 0:
            raise RuntimeError(f"cannot install syscall filter: {code}")
    finally:
        lib.seccomp_release(ctx)
