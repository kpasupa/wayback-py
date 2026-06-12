"""Phase 4: fetch-assets — discover and download assets referenced in clean HTML/CSS
but missing from the manifest, then re-clean affected pages.

Works in waves:
  Wave 1: scan clean HTML for unresolved Wayback src=/href= attributes.
  Wave 2+: scan newly-downloaded CSS files for url() and @import.
  Stops when a wave finds nothing new. Capped at 5 waves.

Within each wave, all downloads run in parallel. Waves are sequential
because CSS must be downloaded before it can be scanned.

Deduplication: assets are keyed by normalize(original_url) so the same
image or stylesheet referenced by 100 pages is downloaded exactly once.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import requests
from bs4 import BeautifulSoup

from .config import Config
from .downloader import HEADERS, RateLimitGate, _save
from .enumerate import load_manifest, manifest_path
from .state import State, DOWNLOADING
from .urls import (
    _dedupe, assign_local_paths, host_of, normalize,
    registrable_suffix_match, split_fragment, unwrap_wayback,
)

MAX_WAVES = 5

# CSS url() and @import patterns (catches quoted and unquoted forms).
_CSS_URL_RE = re.compile(r'url\(\s*["\']?(https?://[^)"\'<>\s]+)["\']?\s*\)', re.I)
_CSS_IMPORT_RE = re.compile(
    r'@import\s+(?:url\(\s*)?["\']?(https?://[^)"\'<>\s]+)["\']?', re.I
)


def _to_id_url(wayback_url: str) -> str:
    """Swap the Wayback content modifier to id_ (raw bytes, no server-side rewriting)."""
    return re.sub(r"(web\.archive\.org/web/\d+)[a-z_]*/", r"\1id_/", wayback_url, count=1)


def _scan_html(clean_dir: Path, known_keys: set[str],
               scope_hosts: set[str], ignore: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Scan clean HTML for src/href attributes still pointing at Wayback URLs.

    Returns {original_url: (wayback_url, found_in_clean_path)}.
    Keyed by original URL so the same asset referenced on many pages appears once.
    """
    found: dict[str, tuple[str, str]] = {}
    for html_file in clean_dir.rglob("*.html"):
        if html_file.name.startswith("_"):
            continue
        soup = BeautifulSoup(
            html_file.read_text(encoding="utf-8", errors="replace"), "html.parser"
        )
        for tag in soup.find_all(True):
            for attr in ("src", "href"):
                val = tag.get(attr) or ""
                if "web.archive.org" not in val:
                    continue
                unwrapped = unwrap_wayback(val)
                if not unwrapped:
                    continue
                base, _ = split_fragment(unwrapped)
                key = normalize(base, ignore)
                if key in known_keys or base in found:
                    continue
                if not any(registrable_suffix_match(host_of(base), h) for h in scope_hosts):
                    continue
                found[base] = (val, str(html_file))
    return found


def _scan_css(raw_dir: Path, css_local_paths: set[str], known_keys: set[str],
              scope_hosts: set[str], ignore: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Scan downloaded CSS files for url() and @import Wayback references.

    Returns {original_url: (wayback_url, found_in_css_path)}.
    """
    found: dict[str, tuple[str, str]] = {}
    for local_path in css_local_paths:
        f = raw_dir / local_path
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for pattern in (_CSS_URL_RE, _CSS_IMPORT_RE):
            for m in pattern.finditer(text):
                val = m.group(1)
                unwrapped = unwrap_wayback(val)
                original = unwrapped or val
                base, _ = split_fragment(original)
                key = normalize(base, ignore)
                if key in known_keys or base in found:
                    continue
                if not any(registrable_suffix_match(host_of(base), h) for h in scope_hosts):
                    continue
                found[base] = (val, str(f))
    return found


def _build_entries(discovered: dict[str, tuple[str, str]], existing: list[dict],
                   target_name: str, ignore: tuple[str, ...]) -> list[dict]:
    """Build manifest entries for discovered assets, deduped against existing paths."""
    used_paths = {e["local_path"] for e in existing}
    path_map = assign_local_paths(list(discovered.keys()), prefix=target_name,
                                  ignore_params=ignore)
    entries = []
    for original, (wayback, found_in) in discovered.items():
        local_path = _dedupe(path_map[original], used_paths)
        used_paths.add(local_path)
        ts_m = re.search(r"/web/(\d+)", wayback)
        entries.append({
            "original": original,
            "key": normalize(original, ignore),
            "timestamp": ts_m.group(1) if ts_m else "",
            "wayback_url": _to_id_url(wayback),
            "local_path": local_path,
            "target": target_name,
            "_found_in": found_in,
        })
    return entries


def _download_one(entry: dict, raw_dir: Path, session: requests.Session,
                  gate: RateLimitGate, delay: float) -> bool:
    dest = raw_dir / entry["local_path"]
    if dest.exists():
        return True
    gate.wait()
    time.sleep(delay + random.uniform(0, delay * 0.5))
    try:
        r = session.get(entry["wayback_url"], headers=HEADERS, timeout=60,
                        allow_redirects=True)
        if r.status_code == 200:
            _save(dest, r.content)
            return True
    except requests.RequestException:
        pass
    return False


def run(config: Config, state: State, only_target: str | None = None) -> None:
    manifest = load_manifest(config.run_dir)
    raw_dir = Path(config.run_dir) / "raw"
    clean_dir = Path(config.run_dir) / "clean"
    ignore = tuple(config.ignore_query_params)

    targets = config.targets if not only_target else [config.target(only_target)]
    scope_hosts = {host_of(t.url) for t in targets}
    default_target = only_target or targets[0].name

    session = requests.Session()
    session.headers.update(HEADERS)
    gate = RateLimitGate(state)

    new_css_paths: set[str] = set()   # CSS downloaded this wave, scanned next wave
    affected_clean: set[str] = set()  # clean HTML files that need re-cleaning
    total_fetched = 0

    for wave in range(1, MAX_WAVES + 1):
        known_keys = {normalize(e["original"], ignore) for e in manifest}

        if wave == 1:
            discovered = _scan_html(clean_dir, known_keys, scope_hosts, ignore)
        else:
            if not new_css_paths:
                print(f"[fetch-assets] wave {wave}: no new CSS to scan — done.")
                break
            discovered = _scan_css(raw_dir, new_css_paths, known_keys, scope_hosts, ignore)
            new_css_paths = set()

        if not discovered:
            print(f"[fetch-assets] wave {wave}: no new assets found — done.")
            break

        print(f"[fetch-assets] wave {wave}: {len(discovered)} new assets")
        new_entries = _build_entries(discovered, manifest, default_target, ignore)

        ok = fail = 0
        lock = threading.Lock()

        def _task(entry: dict) -> None:
            nonlocal ok, fail
            success = _download_one(entry, raw_dir, session, gate, config.request_delay)
            with lock:
                if success:
                    ok += 1
                    if entry["local_path"].endswith(".css"):
                        new_css_paths.add(entry["local_path"])
                    if entry.get("_found_in"):
                        affected_clean.add(entry["_found_in"])
                else:
                    fail += 1

        state.update(phase=DOWNLOADING, total=len(new_entries), downloaded=0)
        with ThreadPoolExecutor(max_workers=config.threads) as pool:
            list(pool.map(_task, new_entries))

        print(f"[fetch-assets] wave {wave}: {ok} ok, {fail} failed")
        total_fetched += ok

        # Strip temp field, add to manifest, persist.
        for e in new_entries:
            e.pop("_found_in", None)
        manifest.extend(new_entries)
        manifest_path(config.run_dir).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    if total_fetched == 0:
        print("[fetch-assets] nothing new to fetch.")
        return

    # Re-clean only the pages that had unresolved assets.
    if affected_clean:
        print(f"[fetch-assets] re-cleaning {len(affected_clean)} affected pages...")
        from . import cleaner
        # Reload manifest so cleaner sees the new entries.
        full_manifest = load_manifest(config.run_dir)
        url_map = {normalize(e["original"], ignore): e["local_path"] for e in full_manifest}
        scope_hosts_all = {host_of(t.url) for t in config.targets}
        localize_by_target = {t.name: t.localize_assets for t in config.targets}

        for clean_path in affected_clean:
            p = Path(clean_path)
            if not p.exists():
                continue
            # Find the manifest entry for this clean path.
            entry = next(
                (e for e in full_manifest
                 if str(Path(config.run_dir) / "clean" / e["local_path"]) == clean_path),
                None,
            )
            if not entry:
                continue
            raw_src = Path(config.run_dir) / "raw" / entry["local_path"]
            if not raw_src.exists():
                continue
            raw = raw_src.read_text(encoding="utf-8", errors="replace")
            this_clean = PurePosixPath(entry["local_path"])
            css_href = cleaner._relpath(this_clean, f"{entry['target']}/{cleaner.STYLE_FILE}")
            js_href = cleaner._relpath(this_clean, f"{entry['target']}/{cleaner.SCRIPT_FILE}")
            out = cleaner.clean_html(
                raw, entry["original"], this_clean, url_map, scope_hosts_all,
                localize_by_target.get(entry["target"], False), ignore,
                css_href, js_href,
            )
            p.write_text(out, encoding="utf-8")

    print(f"[fetch-assets] done — {total_fetched} assets fetched, "
          f"{len(affected_clean)} pages re-cleaned.")
