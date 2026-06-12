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
from .enumerate import (
    external_manifest_path, load_external_manifest, load_manifest, manifest_path,
)
from .state import State, DOWNLOADING, FETCHING_ASSETS
from .urls import (
    _dedupe, assign_local_paths, host_of, normalize,
    registrable_suffix_match, resolve_css_ref, split_fragment, unwrap_wayback,
)

MAX_WAVES = 5

# CSS url() and @import patterns (catches quoted and unquoted forms).
_CSS_URL_RE = re.compile(r'url\(\s*["\']?(https?://[^)"\'<>\s]+)["\']?\s*\)', re.I)
_CSS_IMPORT_RE = re.compile(
    r'@import\s+(?:url\(\s*)?["\']?(https?://[^)"\'<>\s]+)["\']?', re.I
)
# Same, but ALSO match relative/protocol-relative refs (resolved against the CSS URL).
_CSS_URL_ANY_RE = re.compile(r"""url\(\s*['"]?([^)'"]+?)['"]?\s*\)""", re.I)
_CSS_IMPORT_ANY_RE = re.compile(r"""@import\s+(?:url\(\s*)?['"]?([^)'";\s]+)""", re.I)

# Tag/attribute pairs that reference a SUB-RESOURCE (not <a> navigation links).
# Used by the external scanner so it pulls assets/iframes but never crawls links.
_ASSET_REFS = [
    ("img", "src"), ("script", "src"), ("source", "src"),
    ("video", "src"), ("audio", "src"), ("iframe", "src"),
    ("embed", "src"), ("object", "data"),
]
_LINK_RELS = {"stylesheet", "icon", "apple-touch-icon", "shortcut icon"}
# Lazy-load URL attributes used by JS slideshow/image libraries.
_LAZY_ATTRS = ("data-src", "data-original", "data-lazy-src")

_UNSAFE_HOST = re.compile(r"[^A-Za-z0-9._-]")


def _safe_host(host: str) -> str:
    """Sanitize a host into a single safe path segment (e.g. 'h:8080' -> 'h_8080')."""
    return _UNSAFE_HOST.sub("_", host) or "_host"


def _to_id_url(wayback_url: str) -> str:
    """Swap the Wayback content modifier to id_ (raw bytes, no server-side rewriting)."""
    return re.sub(r"(web\.archive\.org/web/\d+)[a-z_]*/", r"\1id_/", wayback_url, count=1)


def _scan_html(clean_dir: Path, known_keys: set[str],
               scope_hosts: set[str], ignore: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Scan clean HTML for asset URLs not yet localized.

    Catches three cases:
    - src/href still pointing at web.archive.org (absolute Wayback-wrapped originals)
    - src/href with plain in-scope http:// URLs (never Wayback-wrapped in the raw page)
    - src/href root-relative /web/TIMESTAMP.../http://... URLs (Wayback-rewritten in raw HTML)

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
                if val.startswith(("http://", "https://")):
                    if "web.archive.org" in val:
                        # Absolute Wayback-wrapped URL: unwrap to get original
                        unwrapped = unwrap_wayback(val)
                        if not unwrapped:
                            continue
                        base, _ = split_fragment(unwrapped)
                        wayback_url = val
                    else:
                        # Plain absolute original URL — build a Wayback fetch URL
                        base, _ = split_fragment(val)
                        wayback_url = f"https://web.archive.org/web/id_/{base}"
                elif val.startswith("/web/"):
                    # Root-relative Wayback URL (e.g. /web/20130101im_/http://...)
                    unwrapped = unwrap_wayback(val)
                    if not unwrapped:
                        continue
                    base, _ = split_fragment(unwrapped)
                    wayback_url = "https://web.archive.org" + val
                else:
                    continue

                key = normalize(base, ignore)
                if key in known_keys or base in found:
                    continue
                if not any(registrable_suffix_match(host_of(base), h) for h in scope_hosts):
                    continue
                found[base] = (wayback_url, str(html_file))
    return found


def _scan_css(raw_dir: Path, css_local_paths: set[str], known_keys: set[str],
              scope_hosts: set[str], ignore: tuple[str, ...],
              external: bool = False) -> dict[str, tuple[str, str]]:
    """Scan downloaded CSS files for url() and @import Wayback references.

    By default keeps only in-scope hosts; with external=True keeps only out-of-scope
    hosts. Returns {original_url: (wayback_url, found_in_css_path)}.
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
                in_scope = any(registrable_suffix_match(host_of(base), h) for h in scope_hosts)
                if in_scope == external:  # skip in-scope when external, and vice versa
                    continue
                found[base] = (val, str(f))
    return found


def _scan_css_external(raw_dir: Path, css_entries: list[dict], known_keys: set[str],
                       scope_hosts: set[str], ignore: tuple[str, ...],
                       exclude_res: tuple = ()) -> dict[str, tuple[str, str, str]]:
    """Scan downloaded CSS for EXTERNAL url()/@import targets, resolving RELATIVE refs.

    Each ref (relative like url(../img/x.png), protocol-relative, absolute, or
    Wayback-wrapped) is resolved against the stylesheet's own original URL, then kept
    if external. The fetch URL is built at the CSS's own timestamp so sprites/preloaders
    are period-correct. Returns {original_url: (wayback_url, "", "asset")}.
    """
    found: dict[str, tuple[str, str, str]] = {}
    for entry in css_entries:
        f = raw_dir / entry["local_path"]
        if not f.exists():
            continue
        css_original = entry["original"]
        ts = entry.get("timestamp", "")
        text = f.read_text(encoding="utf-8", errors="replace")
        for pattern in (_CSS_URL_ANY_RE, _CSS_IMPORT_ANY_RE):
            for m in pattern.finditer(text):
                abs_url = resolve_css_ref(m.group(1), css_original)
                if not abs_url:
                    continue
                base, _ = split_fragment(abs_url)
                key = normalize(base, ignore)
                if key in known_keys or base in found:
                    continue
                host = host_of(base)
                if not host or host == "archive.org" or host.endswith(".archive.org"):
                    continue
                if any(registrable_suffix_match(host, h) for h in scope_hosts):
                    continue  # in scope — not handled here
                if any(r.search(base) for r in exclude_res):
                    continue  # user opted this external URL out
                wb = (f"https://web.archive.org/web/{ts}id_/{base}" if ts
                      else f"https://web.archive.org/web/id_/{base}")
                found[base] = (wb, "", "asset")
    return found


def _consider_external(val: str | None, found_in: str, kind: str,
                       found: dict[str, tuple[str, str, str]], scope_hosts: set[str],
                       known_keys: set[str], ignore: tuple[str, ...],
                       exclude_res: tuple = ()) -> None:
    """Classify one src/href value; if it is an EXTERNAL Wayback resource, record it.

    Resolves Wayback-wrapped absolute, plain absolute, and root-relative /web/ forms
    to an original URL + a fetchable Wayback URL, then keeps it only when the host is
    out of scope and not already known.
    """
    if not val:
        return
    val = val.strip()
    if val.startswith(("http://", "https://")):
        if "web.archive.org" in val:
            unwrapped = unwrap_wayback(val)
            if not unwrapped:
                return
            base, _ = split_fragment(unwrapped)
            wayback_url = val
        else:
            base, _ = split_fragment(val)
            wayback_url = f"https://web.archive.org/web/id_/{base}"
    elif val.startswith("/web/"):
        unwrapped = unwrap_wayback(val)
        if not unwrapped:
            return
        base, _ = split_fragment(unwrapped)
        wayback_url = "https://web.archive.org" + val
    else:
        return  # relative, data:, mailto:, anchors — not a fetchable external resource
    if base in found:
        return
    host = host_of(base)
    if not host or host == "archive.org" or host.endswith(".archive.org"):
        return  # Wayback infrastructure (toolbar JS/CSS), not original content
    if any(registrable_suffix_match(host, h) for h in scope_hosts):
        return  # in scope — handled by the normal manifest, not here
    if any(r.search(base) for r in exclude_res):
        return  # user opted this external URL out (e.g. social-widget iframes)
    if normalize(base, ignore) in known_keys:
        return
    found[base] = (wayback_url, found_in, kind)


def _scan_html_external(files: list[tuple[Path, str]], scope_hosts: set[str],
                        known_keys: set[str], ignore: tuple[str, ...],
                        exclude_res: tuple = ()) -> dict[str, tuple[str, str, str]]:
    """Scan HTML files for EXTERNAL sub-resource references (assets + iframes).

    `files` is a list of (path_to_read, found_in_label) — the label is the clean-HTML
    path to re-clean when this reference is localized. Only asset-bearing tags and
    <iframe> are inspected; <a> navigation links are ignored (the iframe boundary).
    Returns {original_url: (wayback_url, found_in, kind)}.
    """
    found: dict[str, tuple[str, str, str]] = {}
    for read_path, found_in in files:
        p = Path(read_path)
        if not p.exists() or p.name.startswith("_"):
            continue
        soup = BeautifulSoup(p.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for tag, attr in _ASSET_REFS:
            kind = "iframe" if tag == "iframe" else "asset"
            for el in soup.find_all(tag):
                _consider_external(el.get(attr), found_in, kind, found,
                                   scope_hosts, known_keys, ignore, exclude_res)
        for el in soup.find_all("link", href=True):
            if set(el.get("rel") or []) & _LINK_RELS:
                _consider_external(el.get("href"), found_in, "asset", found,
                                   scope_hosts, known_keys, ignore, exclude_res)
        # Lazy-load attributes (e.g. royalSlider's data-src) — images set by JS at
        # runtime, so they never appear in a plain src=.
        for attr in _LAZY_ATTRS:
            for el in soup.find_all(attrs={attr: True}):
                _consider_external(el.get(attr), found_in, "asset", found,
                                   scope_hosts, known_keys, ignore, exclude_res)
        # CSS url() in <style> blocks and inline style="" attributes.
        for st in soup.find_all("style"):
            for m in _CSS_URL_RE.finditer(st.string or ""):
                _consider_external(m.group(1), found_in, "asset", found,
                                   scope_hosts, known_keys, ignore, exclude_res)
        for el in soup.find_all(style=True):
            for m in _CSS_URL_RE.finditer(el.get("style") or ""):
                _consider_external(m.group(1), found_in, "asset", found,
                                   scope_hosts, known_keys, ignore, exclude_res)
    return found


def _build_external_entries(discovered: dict[str, tuple[str, str, str]],
                            existing_ext: list[dict], default_target: str,
                            ignore: tuple[str, ...]) -> list[dict]:
    """Build manifest_external entries, namespaced under <target>/_external/<host>/."""
    used_paths = {e["local_path"] for e in existing_ext}
    by_host: dict[str, list[str]] = {}
    for original in discovered:
        by_host.setdefault(host_of(original), []).append(original)
    path_map: dict[str, str] = {}
    for host, urls in by_host.items():
        prefix = f"{default_target}/_external/{_safe_host(host)}"
        path_map.update(assign_local_paths(urls, prefix=prefix, ignore_params=ignore))

    entries = []
    for original, (wayback, found_in, kind) in discovered.items():
        local_path = _dedupe(path_map[original], used_paths)
        used_paths.add(local_path)
        ts_m = re.search(r"/web/(\d+)", wayback)
        ts = ts_m.group(1) if ts_m else ""
        if kind == "iframe":
            # Fetch the embedded page via NORMAL playback (no id_) so Wayback rewrites
            # its asset URLs — src, data-src, style url() — to timestamped archive URLs
            # the next wave can discover. id_ would return raw, unrewritten plain URLs.
            fetch_url = (f"https://web.archive.org/web/{ts}/{original}" if ts
                         else f"https://web.archive.org/web/{original}")
        else:
            fetch_url = _to_id_url(wayback)
        entries.append({
            "original": original,
            "key": normalize(original, ignore),
            "timestamp": ts,
            "wayback_url": fetch_url,
            "local_path": local_path,
            "target": default_target,
            "kind": kind,
            "_found_in": found_in,
        })
    return entries


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
    except (requests.RequestException, OSError):
        # OSError covers a too-long Windows path for query-heavy URLs — skip that one
        # resource rather than aborting the whole run.
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

        state.update(phase=FETCHING_ASSETS, target=only_target or "")
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
            state.incr(downloaded=1 if success else 0, failed=0 if success else 1)
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
                css_href, js_href, config.kill_redirects,
            )
            p.write_text(out, encoding="utf-8")

    print(f"[fetch-assets] done — {total_fetched} assets fetched, "
          f"{len(affected_clean)} pages re-cleaned.")


def run_external(config: Config, state: State, only_target: str | None = None) -> int:
    """Discover and fetch EXTERNAL-domain resources referenced by target pages.

    Scans the raw captures for sub-resources (img/script/link/iframe/embed/object +
    CSS url()/@import) on out-of-scope hosts, fetches them from Wayback into
    manifest_external.json under <target>/_external/<host>/, and works in waves so an
    iframe is fetched as a page and then its own direct assets are pulled next wave.
    Does NOT clean — the caller runs `clean` afterward (cleaner reads both manifests).
    Returns the number of resources fetched. Idempotent: known/on-disk items are skipped.
    """
    run_dir = config.run_dir
    raw_dir = Path(run_dir) / "raw"
    clean_dir = Path(run_dir) / "clean"
    ignore = tuple(config.ignore_query_params)

    manifest = load_manifest(run_dir)
    if only_target:
        manifest = [e for e in manifest if e["target"] == only_target]
    ext_manifest = load_external_manifest(run_dir)

    targets = config.targets if not only_target else [config.target(only_target)]
    scope_hosts = {host_of(t.url) for t in targets}
    default_target = only_target or targets[0].name
    exclude_res = tuple(re.compile(p) for p in config.external_exclude)

    session = requests.Session()
    session.headers.update(HEADERS)
    gate = RateLimitGate(state)
    from .cleaner import _is_binary, _sniff_css  # detect stylesheets by content

    affected_clean: set[str] = set()
    new_css_entries: list[dict] = []
    new_iframe_html: set[str] = set()
    total = 0

    # Wave 1 scans every downloaded main page's RAW capture (it still holds the
    # original external references); found_in points at the clean page to re-clean.
    page_files = [
        (raw_dir / e["local_path"], str(clean_dir / e["local_path"]))
        for e in manifest
        if e["local_path"].endswith(".html") and (raw_dir / e["local_path"]).exists()
    ]

    for wave in range(1, MAX_WAVES + 1):
        known = ({normalize(e["original"], ignore) for e in manifest}
                 | {normalize(e["original"], ignore) for e in ext_manifest})
        state.update(phase=FETCHING_ASSETS, target=only_target or "")

        if wave == 1:
            discovered = _scan_html_external(page_files, scope_hosts, known, ignore,
                                             exclude_res)
        else:
            # Later waves scan what the previous wave produced: iframe HTML (for its
            # own assets — assets only, never <a> links) and CSS (url()/@import).
            discovered = {}
            if new_iframe_html:
                iframe_files = [(raw_dir / lp, "") for lp in new_iframe_html]
                discovered.update(
                    _scan_html_external(iframe_files, scope_hosts, known, ignore,
                                        exclude_res))
            if new_css_entries:
                discovered.update(_scan_css_external(
                    raw_dir, new_css_entries, known, scope_hosts, ignore, exclude_res))
            new_css_entries, new_iframe_html = [], set()

        if not discovered:
            print(f"[fetch-external] wave {wave}: no new external resources — done.")
            break

        n_if = sum(1 for v in discovered.values() if v[2] == "iframe")
        print(f"[fetch-external] wave {wave}: {len(discovered)} new "
              f"({n_if} iframes, {len(discovered) - n_if} assets)")
        new_entries = _build_external_entries(discovered, ext_manifest,
                                              default_target, ignore)

        ok = fail = 0
        lock = threading.Lock()

        def _task(entry: dict) -> None:
            nonlocal ok, fail
            success = _download_one(entry, raw_dir, session, gate, config.request_delay)
            state.incr(downloaded=1 if success else 0, failed=0 if success else 1)
            with lock:
                if success:
                    ok += 1
                    lp = entry["local_path"]
                    if entry["kind"] == "iframe" and lp.endswith(".html"):
                        new_iframe_html.add(lp)
                    else:
                        # Detect stylesheets by content (query-string CSS is named
                        # .html), so the next wave can resolve their url()/@import.
                        try:
                            data = (raw_dir / lp).read_bytes()
                        except OSError:
                            data = b""
                        if data and not _is_binary(data, lp) and _sniff_css(data):
                            new_css_entries.append(entry)
                    if entry.get("_found_in"):
                        affected_clean.add(entry["_found_in"])
                else:
                    fail += 1

        state.update(phase=DOWNLOADING, total=len(new_entries), downloaded=0)
        with ThreadPoolExecutor(max_workers=config.threads) as pool:
            list(pool.map(_task, new_entries))
        print(f"[fetch-external] wave {wave}: {ok} ok, {fail} failed")
        total += ok

        for e in new_entries:
            e.pop("_found_in", None)
        ext_manifest.extend(new_entries)
        external_manifest_path(run_dir).write_text(
            json.dumps(ext_manifest, indent=2), encoding="utf-8")

    if total == 0:
        print("[fetch-external] nothing new to fetch.")
        return 0

    # Delete cleaned pages that referenced newly-fetched externals so the next clean
    # regenerates them with those resources localized. When clean hasn't run yet
    # (e.g. inside `run`) these files don't exist and this is a no-op.
    for clean_path in affected_clean:
        p = Path(clean_path)
        if p.exists():
            p.unlink()

    print(f"[fetch-external] done — {total} external resources fetched.")
    return total
