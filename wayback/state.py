"""Shared run state, persisted atomically to state.json.

The running phase writes this file; `wayback status` (a separate process) reads it.
Communication is purely through the file, so it works across terminals with no
shared memory. Writes are atomic (temp file + os.replace) so a reader never sees
a half-written file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = "state.json"

# Phases shown by `status`.
ENUMERATING = "enumerating"
DOWNLOADING = "downloading"
WAITING_RATE_LIMIT = "waiting_rate_limit"
CLEANING = "cleaning"
DONE = "done"
IDLE = "idle"

PHASE_LABELS = {
    ENUMERATING: "Enumerating",
    DOWNLOADING: "Downloading",
    WAITING_RATE_LIMIT: "Waiting for rate limit",
    CLEANING: "Cleaning",
    DONE: "Idle",
    IDLE: "Idle",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RateLimit:
    waiting: bool = False
    resume_at: str | None = None
    reason: str | None = None


@dataclass
class StateData:
    pid: int = field(default_factory=os.getpid)
    heartbeat: str = field(default_factory=_now_iso)
    phase: str = IDLE
    target: str = ""
    total: int = 0
    downloaded: int = 0
    failed: int = 0
    cleaned: int = 0
    clean_total: int = 0
    rate_limit: RateLimit = field(default_factory=RateLimit)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def state_path(run_dir: Path) -> Path:
    return Path(run_dir) / STATE_FILENAME


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        # On Windows os.replace fails with PermissionError if a reader (e.g. the
        # status command/status.bat) has the file open for that instant. The lock
        # is transient, so retry briefly before giving up.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class State:
    """Thread-safe, atomically-persisted run state used by the active process."""

    # Minimum seconds between disk writes for high-frequency updates (counts/
    # heartbeats). Important changes (phase, rate-limit) bypass this and flush now.
    MIN_FLUSH_INTERVAL = 0.5

    def __init__(self, run_dir: Path, on_flush=None):
        self._path = state_path(run_dir)
        self._lock = threading.Lock()
        self._data = StateData()
        self._on_flush = on_flush  # optional callback(dict) -> e.g. Google Sheets push
        self._last_flush = 0.0

    def update(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self._data, k, v)
            self._data.heartbeat = _now_iso()
            self._flush_locked(force=True)  # phase/total changes are significant

    def set_rate_limit(self, waiting: bool, reason: str | None = None,
                       resume_at: str | None = None) -> None:
        with self._lock:
            self._data.rate_limit = RateLimit(waiting=waiting, reason=reason,
                                              resume_at=resume_at)
            self._data.phase = WAITING_RATE_LIMIT if waiting else self._data.phase
            self._data.heartbeat = _now_iso()
            self._flush_locked(force=True)

    def incr(self, *, downloaded: int = 0, failed: int = 0, cleaned: int = 0) -> None:
        with self._lock:
            self._data.downloaded += downloaded
            self._data.failed += failed
            self._data.cleaned += cleaned
            self._data.heartbeat = _now_iso()
            self._flush_locked()  # throttled — counts can lag the disk by <1s

    def heartbeat(self) -> None:
        with self._lock:
            self._data.heartbeat = _now_iso()
            self._flush_locked()

    def flush(self) -> None:
        """Force the in-memory state to disk now (e.g. at end of a phase)."""
        with self._lock:
            self._flush_locked(force=True)

    def _flush_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_flush) < self.MIN_FLUSH_INTERVAL:
            return  # in-memory state already updated; skip this disk write
        self._last_flush = now
        payload = self._data.to_dict()
        _atomic_write(self._path, payload)
        if self._on_flush:
            try:
                self._on_flush(payload)
            except Exception:
                pass  # reporting must never crash the run


def read_state(run_dir: Path) -> dict | None:
    """Read state.json for the `status` command. Returns None if absent/corrupt."""
    path = state_path(run_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # optional, more reliable
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        # No os.kill(pid, 0) semantics on Windows; use tasklist as a fallback.
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in out.stdout
        except Exception:
            return True  # can't tell; assume alive rather than report false death
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # process exists, we just can't signal it
    except (ProcessLookupError, OSError):
        return False


def liveness(state: dict) -> str:
    """Return 'alive', 'dead', or 'idle' from a state dict.

    Primary signal is PID liveness: a running process is 'alive' even during a long
    CDX page fetch or rate-limit wait (when no heartbeat is written). 'idle' is a
    clean finish; 'dead' means the process is gone but didn't finish.
    """
    phase = state.get("phase", IDLE)
    if phase in (DONE, IDLE):
        return "idle"
    pid = int(state.get("pid", 0) or 0)
    return "alive" if _pid_alive(pid) else "dead"
