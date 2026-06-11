"""Phase 2: download manifest entries to raw/, resumably.

Threaded with a global rate-limit gate: when any worker hits a block, all workers
pause until a prober confirms the archive is reachable again, so we never hammer
Wayback while it is throttling us.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import cdx
from .config import Config
from .enumerate import load_manifest
from .state import State, DOWNLOADING, DONE

HEADERS = {
    "User-Agent": "wayback-py/1.0 (+archival-research)",
    # Ask for uncompressed bytes; Wayback occasionally mislabels content-encoding.
    "Accept-Encoding": "identity",
}


class RateLimitGate:
    """Lets all workers proceed, but a single block pauses everyone until recovery."""

    def __init__(self, state: State):
        self._state = state
        self._open = threading.Event()
        self._open.set()
        self._lock = threading.Lock()

    def wait(self) -> None:
        self._open.wait()

    def pause_and_recover(self, reason: str, base_wait: float) -> None:
        # Only the first worker to grab the lock runs the recovery loop.
        if not self._lock.acquire(blocking=False):
            self._open.wait()  # another worker is already recovering
            return
        try:
            self._open.clear()
            wait = base_wait
            while True:
                resume_at = (datetime.now(timezone.utc)
                             + timedelta(seconds=wait)).isoformat(timespec="seconds")
                self._state.set_rate_limit(True, reason=reason, resume_at=resume_at)
                print(f"  [blocked:{reason}] pausing all workers {int(wait)}s, "
                      f"then probing CDX...")
                time.sleep(wait)
                if cdx.ping():
                    print("  [recovered] resuming downloads")
                    self._state.set_rate_limit(False)
                    self._state.update(phase=DOWNLOADING)
                    return
                wait = min(wait * 2, 1800)  # cap long block backoff at 30 min
        finally:
            self._open.set()
            self._lock.release()


def _save(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _download_one(entry: dict, session: requests.Session, gate: RateLimitGate,
                  state: State, config: Config, raw_dir: Path) -> str:
    """Return 'skip' | 'ok' | 'fail'. Updates state counters."""
    dest = raw_dir / entry["local_path"]
    if dest.exists():
        return "skip"

    wait = max(config.request_delay, 1.0)
    for attempt in range(1, config.max_retries + 1):
        gate.wait()
        time.sleep(config.request_delay + random.uniform(0, config.request_delay))
        try:
            r = session.get(entry["wayback_url"], headers=HEADERS, timeout=90,
                            allow_redirects=True)
            if r.status_code in (429, 503):
                retry_after = r.headers.get("Retry-After")
                if (retry_after or "").isdigit():
                    time.sleep(min(int(retry_after), 300))
                else:
                    time.sleep(wait)
                    wait = min(wait * 2, 300)
                continue
            if r.status_code == 200:
                _save(dest, r.content)
                state.incr(downloaded=1)
                return "ok"
            # 404 and other terminal statuses: give up on this URL.
            state.incr(failed=1)
            return "fail"
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            # Likely an IP-level block — pause everyone and probe before retrying.
            gate.pause_and_recover(reason=type(exc).__name__, base_wait=900)
            continue
        except requests.exceptions.RequestException as exc:
            # Per-URL quirk (e.g. bad content-encoding, chunked drop). Back off and
            # retry a few times; if it persists, mark this one URL failed and move on.
            print(f"  [warn] {type(exc).__name__} on {entry['original']}, retry {attempt}")
            time.sleep(wait)
            wait = min(wait * 2, 120)
            continue
    state.incr(failed=1)
    return "fail"


def run(config: Config, state: State, only_target: str | None = None) -> dict:
    manifest = load_manifest(config.run_dir)
    if only_target:
        manifest = [e for e in manifest if e["target"] == only_target]

    raw_dir = Path(config.run_dir) / "raw"
    already = sum(1 for e in manifest if (raw_dir / e["local_path"]).exists())

    state.update(phase=DOWNLOADING, total=len(manifest), downloaded=already, failed=0,
                 target=only_target or "")
    print(f"[download] {len(manifest)} URLs, {already} already on disk, "
          f"{config.threads} workers")

    gate = RateLimitGate(state)
    session = requests.Session()
    session.headers.update(HEADERS)

    counts = {"ok": 0, "skip": already, "fail": 0}
    counts_lock = threading.Lock()

    def task(entry: dict) -> None:
        try:
            result = _download_one(entry, session, gate, state, config, raw_dir)
        except Exception as exc:  # noqa: BLE001 — one bad URL must never abort the run
            print(f"  [error] {type(exc).__name__} on {entry['original']}: {exc}")
            state.incr(failed=1)
            result = "fail"
        with counts_lock:
            if result != "skip":
                counts[result] += 1

    pending = [e for e in manifest if not (raw_dir / e["local_path"]).exists()]
    with ThreadPoolExecutor(max_workers=config.threads) as pool:
        list(pool.map(task, pending))

    state.update(phase=DONE)
    state.flush()
    print(f"[download] done: {counts['ok']} new, {counts['skip']} skipped, "
          f"{counts['fail']} failed")
    return counts
