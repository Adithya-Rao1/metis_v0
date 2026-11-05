"""Killable child processes for Lead Studio (sandbox for runs and for molecule parsing).

A child is a fresh Python interpreter (nothing is forked, so no threads, locks or database connections of the API
process are copied) started with `os.posix_spawn` and an EXPLICIT minimal environment: only an allow-list of
harmless variables reaches it, never the database URL, the session key or any other Metis secret. It runs in its own
process group, so `kill()` ends it together with anything it started. It talks to the parent over two pipes: its
stdin (parent -> child, one JSON line per message) and file descriptor 3 (child -> parent, one JSON line per
message). stdout and stderr go to /dev/null, so nothing a molecule makes the native code print can reach a log.

The child kills itself when the parent goes away (stdin reaches end-of-file), when its own wall-clock limit passes
(SIGALRM) and, where the OS supports RLIMIT_AS, when it exceeds its address-space limit.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import os
import select
import signal
import sys
import threading
import time

EVENT_FD = 3  # child -> parent
MAX_LINE = 16 * 1024 * 1024  # a single message above this is a misbehaving child: it is killed

# Variables a child may inherit. Everything else (METIS_*, database and storage credentials, ...) is dropped.
_ENV_ALLOW = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "SYSTEMROOT",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "HF_HOME",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
)
_SECRET_WORDS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "DATABASE", "DSN")


def child_env() -> dict[str, str]:
    """The complete environment of a child: allow-listed variables, the non-secret `STUDIO_*` tuning variables and
    the import path (so the child finds the same code as the API process)."""
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    for k, v in os.environ.items():
        if k.startswith("STUDIO_") and not any(w in k for w in _SECRET_WORDS):
            env[k] = v
    paths = [os.getcwd(), *(p for p in sys.path if p and os.path.isabs(p))]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PATH", "/usr/bin:/bin")
    return env


_LIVE: set[Child] = set()
_LIVE_LOCK = threading.Lock()


def live_children() -> int:
    with _LIVE_LOCK:
        return sum(1 for c in _LIVE if c.alive())


def kill_all() -> None:
    with _LIVE_LOCK:
        kids = list(_LIVE)
    for c in kids:
        c.kill()


atexit.register(kill_all)


class ChildGone(Exception):
    """The child closed its output (it exited or crashed)."""


class ChildMisbehaved(Exception):
    """The child sent a message above MAX_LINE."""


class Child:
    """One spawned child process. `read_line` and `send` are for one thread at a time."""

    def __init__(self, pid: int, wfd: int, rfd: int):
        self.pid, self._wfd, self._rfd = pid, wfd, rfd
        self.returncode: int | None = None
        self._buf = bytearray()
        self._closed = False
        self._lock = threading.Lock()
        os.set_blocking(self._wfd, False)
        os.set_blocking(self._rfd, False)
        with _LIVE_LOCK:
            _LIVE.add(self)

    # ------------------------------------------------------------------ start
    @classmethod
    def spawn(cls, module: str, args: list[str] | None = None) -> Child:
        """Start `python -m module args...` with the minimal environment and the two pipes described above."""
        in_r, in_w = os.pipe()
        ev_r, ev_w = os.pipe()
        try:
            actions = [
                (os.POSIX_SPAWN_DUP2, in_r, 0),
                (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_DUP2, ev_w, EVENT_FD),
            ]
            argv = [sys.executable, "-u", "-m", module, *(args or [])]
            pid = os.posix_spawn(sys.executable, argv, child_env(), file_actions=actions, setpgroup=0)
        except BaseException:
            os.close(in_w)
            os.close(ev_r)
            raise
        finally:
            os.close(in_r)
            os.close(ev_w)
        return cls(pid, in_w, ev_r)

    # ------------------------------------------------------------------ io
    def send(self, data: bytes, timeout: float) -> None:
        """Write all of `data` to the child's stdin within `timeout` seconds (a child that does not read is a
        hung child: raises TimeoutError)."""
        view, deadline = memoryview(data), time.monotonic() + timeout
        while view:
            try:
                n = os.write(self._wfd, view)
                view = view[n:]
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError from None
                select.select([], [self._wfd], [], min(left, 0.2))
            except (BrokenPipeError, OSError) as exc:
                raise ChildGone from exc

    def read_line(self, timeout: float) -> bytes | None:
        """The next message from the child, None if none arrived within `timeout`. Raises ChildGone at end of
        output (any message already buffered is delivered first)."""
        deadline = time.monotonic() + timeout
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                line = bytes(self._buf[:i])
                del self._buf[: i + 1]
                return line
            if len(self._buf) > MAX_LINE:
                raise ChildMisbehaved
            left = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self._rfd], [], [], min(left, 0.5))
            if not ready:
                if time.monotonic() >= deadline:
                    return None
                continue
            try:
                chunk = os.read(self._rfd, 1 << 16)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise ChildGone from exc
            if not chunk:
                raise ChildGone
            self._buf += chunk

    # ------------------------------------------------------------------ lifecycle
    def poll(self) -> int | None:
        with self._lock:
            if self.returncode is None:
                try:
                    pid, status = os.waitpid(self.pid, os.WNOHANG)
                except ChildProcessError:
                    self.returncode = -1
                else:
                    if pid:
                        self.returncode = os.waitstatus_to_exitcode(status)
            return self.returncode

    def alive(self) -> bool:
        return self.poll() is None

    def kill(self) -> None:
        """SIGKILL the child's whole process group and reap it. Safe to call repeatedly and from any thread."""
        if self.poll() is None:
            for send in (
                lambda: os.killpg(self.pid, signal.SIGKILL),
                lambda: os.kill(self.pid, signal.SIGKILL),
            ):
                try:
                    send()
                    break
                except ProcessLookupError:
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EPERM, errno.ESRCH):
                        raise
            deadline = time.monotonic() + 5
            while self.poll() is None and time.monotonic() < deadline:
                time.sleep(0.005)
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for fd in (self._wfd, self._rfd):
            with contextlib.suppress(OSError):
                os.close(fd)
        with _LIVE_LOCK:
            _LIVE.discard(self)


# ====================================================================================== child side
def limit_address_space(mb: int) -> None:
    """Cap the child's address space (RLIMIT_AS). Not every OS honours it (macOS ignores or refuses it): then the
    wall-clock limit and the parent's kill are the only bounds, and that is documented."""
    if mb <= 0:
        return
    try:
        import resource

        limit = mb * 1024 * 1024
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ImportError, ValueError, OSError):
        pass


def disable_core_dumps() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass


def hard_deadline(seconds: float) -> None:
    """Terminate this process after `seconds` even if the parent never comes back."""
    if seconds > 0:
        signal.alarm(max(1, int(seconds) + 1))


def read_stdin_line(limit: int = MAX_LINE) -> bytes | None:
    """One line from file descriptor 0 (None at end-of-file). Unbuffered, so a later reader of fd 0 sees the rest."""
    out = bytearray()
    while True:
        b = os.read(0, 1)
        if not b:
            return None
        if b == b"\n":
            return bytes(out)
        out += b
        if len(out) > limit:
            return None


def write_event_line(data: bytes) -> None:
    """Write one message (newline-terminated) to the parent; blocks while the pipe is full (back-pressure)."""
    view = memoryview(data + b"\n")
    while view:
        view = view[os.write(EVENT_FD, view) :]


def exit_when_parent_gone() -> None:
    """Background thread: file descriptor 0 reaching end-of-file means the API process is gone."""

    def watch() -> None:
        try:
            while os.read(0, 4096):
                pass
        finally:
            os._exit(70)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
