"""Phase 3: turn raw archived HTML into clean, self-contained offline docs.

Link rewriting is driven entirely by manifest.json (original URL -> local path), so
it never guesses from the filesystem. Each link is classified as local (in manifest),
dead (in-scope but never captured), or external (left absolute).
"""

from __future__ import annotations

import os
import re
import time
import warnings
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup, Comment, MarkupResemblesLocatorWarning
from bs4 import XMLParsedAsHTMLWarning

# Some captures are XML feeds or near-empty responses; parsing them with the HTML
# parser is fine for our link-rewriting purposes, so quiet these warnings.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

from .config import Config
from .enumerate import load_manifest
from .state import State, CLEANING, DONE
from .urls import (host_of, normalize, registrable_suffix_match, resolve_href,
                   split_fragment)

# Wayback toolbar element ids and a script/style signature (defensive — `id_` raw
# captures usually have none, but mixed captures sometimes do).
WAYBACK_IDS = {
    "wm-ipp", "wm-ipp-base", "wm-ipp-print", "donato", "playback",
    "wm-share", "wm-tabs", "wm-toolbar",
}
WAYBACK_SCRIPT_RE = re.compile(r"archive\.org|__wm\.|wombat|WB_wombat|RufflePlayer", re.I)
DEAD_LOCAL_RE = re.compile(r"^/(skins|extensions|opensearch_desc|favicon\.ico)")

BASE_CSS = """/* wayback-py shared stylesheet — edit this one file to restyle every page. */
:root{--fg:#1a1a2e;--text:#24292e;--muted:#586069;--accent:#1a73e8;
      --border:#e1e4e8;--bg:#fff;--code-bg:#f6f8fa;}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
     max-width:880px;margin:0 auto;padding:32px 20px 80px;line-height:1.65;
     color:var(--text);background:var(--bg);}
h1,h2,h3,h4{color:var(--fg);line-height:1.25;margin-top:1.6em}
h1{font-size:1.9em;border-bottom:2px solid var(--border);padding-bottom:.3em}
h2{font-size:1.45em;border-bottom:1px solid var(--border);padding-bottom:.25em}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
a.dead{color:#b0b0b0;text-decoration:line-through;cursor:not-allowed}
code,pre{font-family:"SF Mono",Consolas,Menlo,monospace;font-size:.9em}
code{background:var(--code-bg);padding:.15em .4em;border-radius:4px}
pre{background:var(--code-bg);padding:14px 16px;border-radius:6px;overflow-x:auto;
    border:1px solid var(--border)}
pre code{background:none;padding:0}
table{border-collapse:collapse;width:100%;margin:1.2em 0}
th,td{border:1px solid var(--border);padding:8px 12px;text-align:left}
th{background:var(--code-bg);font-weight:600}
blockquote{margin:1em 0;padding:.4em 1em;color:var(--muted);
           border-left:4px solid var(--border)}
img{max-width:100%;height:auto}
hr{border:none;border-top:1px solid var(--border);margin:2em 0}
"""

# Shared per-target script loaded on every page. Edit clean/<target>/site.js to
# change behaviour across the whole site WITHOUT re-cleaning. Default: add a
# responsive viewport meta tag (and a clearly-marked spot for your own tweaks).
SITE_JS = """/* site.js - shared script for every page in this folder.
   Edit this one file to affect the whole site (no re-clean needed). */
(function () {
  if (!document.querySelector('meta[name="viewport"]')) {
    var m = document.createElement('meta');
    m.name = 'viewport';
    m.content = 'width=device-width, initial-scale=1.0';
    document.head.appendChild(m);
  }
  /* --- add your own site-wide tweaks below --- */
})();
"""


def _relpath(from_clean: PurePosixPath, to_clean: str) -> str:
    rel = os.path.relpath(to_clean, start=str(from_clean.parent))
    return PurePosixPath(rel.replace("\\", "/")).as_posix()


def _strip_wayback(soup: BeautifulSoup) -> None:
    for wm_id in WAYBACK_IDS:
        for tag in soup.find_all(id=wm_id):
            tag.decompose()
    for tag in soup.find_all(["script", "style", "link"]):
        sig = (tag.get("src") or tag.get("href") or "") + (tag.string or "")
        if WAYBACK_SCRIPT_RE.search(sig) or DEAD_LOCAL_RE.search(tag.get("href") or ""):
            tag.decompose()
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if "archive.org" in c or "FILE ARCHIVED" in c or "WAYBACK" in c.upper():
            c.extract()


def _rewrite_attr(tag, attr: str, page_original: str, this_clean: PurePosixPath,
                  url_map: dict[str, str], ignore_params: tuple[str, ...]) -> None:
    """Rewrite one src/href attribute to a local relative path if the asset is in the manifest."""
    resolved = resolve_href(tag[attr], page_original)
    if not resolved:
        return
    base, frag = split_fragment(resolved)
    target_clean = url_map.get(normalize(base, ignore_params))
    if target_clean:
        tag[attr] = _relpath(this_clean, target_clean) + frag


def _rewrite_assets(soup: BeautifulSoup, page_original: str, this_clean: PurePosixPath,
                    url_map: dict[str, str], ignore_params: tuple[str, ...]) -> None:
    """Rewrite img/script/stylesheet src attributes to local paths for downloaded assets."""
    for tag in soup.find_all(["img", "script", "source", "video", "audio"], src=True):
        _rewrite_attr(tag, "src", page_original, this_clean, url_map, ignore_params)
    for tag in soup.find_all("link", href=True):
        if "stylesheet" in (tag.get("rel") or []):
            _rewrite_attr(tag, "href", page_original, this_clean, url_map, ignore_params)


def _rewrite_links(soup: BeautifulSoup, page_original: str, this_clean: PurePosixPath,
                   url_map: dict[str, str], scope_hosts: set[str],
                   ignore_params: tuple[str, ...]) -> None:
    for a in soup.find_all("a", href=True):
        resolved = resolve_href(a["href"], page_original)
        if not resolved:
            continue  # same-page #anchor / mailto / js -> leave untouched
        base, frag = split_fragment(resolved)
        target_clean = url_map.get(normalize(base, ignore_params))
        if target_clean:
            # In scope and captured -> local relative path, fragment preserved.
            a["href"] = _relpath(this_clean, target_clean) + frag
            continue
        host = host_of(base)
        if any(registrable_suffix_match(host, h) for h in scope_hosts):
            # In scope but never captured -> mark dead.
            a["data-broken"] = base
            a["href"] = "#"
            existing = a.get("class", [])
            a["class"] = existing + ["dead"] if existing else ["dead"]
        # Out of scope -> leave the href exactly as-is (may be a working Wayback link).


def clean_html(html: str, page_original: str, this_clean: PurePosixPath,
               url_map: dict[str, str], scope_hosts: set[str], localize: bool,
               ignore_params: tuple[str, ...], css_href: str, js_href: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    _strip_wayback(soup)
    _rewrite_links(soup, page_original, this_clean, url_map, scope_hosts, ignore_params)
    if localize:
        _rewrite_assets(soup, page_original, this_clean, url_map, ignore_params)

    head = soup.find("head")
    if not head:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)
    for base in soup.find_all("base"):
        base.decompose()
    # Link to the one shared stylesheet (relative path) instead of inlining CSS, so
    # all pages share one editable style.css.
    link = soup.new_tag("link", rel="stylesheet", href=css_href)
    head.append(link)
    # Load the one shared per-target script. Its default injects a responsive
    # viewport meta; edit clean/<target>/site.js to change the whole site (no re-clean).
    script = soup.new_tag("script", src=js_href)
    head.append(script)
    return str(soup)


def _title_of(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    t = soup.find("title") or soup.find("h1")
    return (t.get_text(strip=True) if t else "") or fallback


def build_index(clean_target_dir: Path, label: str, pages: list[tuple[str, str]]) -> None:
    rows = "\n".join(
        f'<tr><td><a href="{path}">{title}</a></td></tr>' for path, title in sorted(pages)
    )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{label} — Index</title>
<style>{BASE_CSS}</style></head>
<body><h1>{label}</h1><p>{len(pages)} pages</p>
<table><thead><tr><th>Page</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""
    (clean_target_dir / "_index.html").write_text(html, encoding="utf-8")


def run(config: Config, state: State, only_target: str | None = None,
        force: bool = False) -> int:
    manifest = load_manifest(config.run_dir)
    if only_target:
        manifest = [e for e in manifest if e["target"] == only_target]

    raw_dir = Path(config.run_dir) / "raw"
    clean_dir = Path(config.run_dir) / "clean"
    ignore = tuple(config.ignore_query_params)
    # Key off normalize(original), not the stored 'key', so the lookup always uses
    # the current normalization on both the manifest side and the link side.
    url_map = {normalize(e["original"], ignore): e["local_path"] for e in manifest}
    scope_hosts = {host_of(t.url) for t in config.targets}
    localize_by_target = {t.name: t.localize_assets for t in config.targets}

    # One stylesheet + one script PER TARGET folder, so each target folder
    # (e.g. clean/devdocs/) is self-contained and can be served as a web root.
    # Both are only (re)written when missing or on --force, so your edits survive
    # normal incremental cleans.
    clean_dir.mkdir(parents=True, exist_ok=True)
    for tname in {e["target"] for e in manifest}:
        tdir = clean_dir / tname
        tdir.mkdir(parents=True, exist_ok=True)
        sp = tdir / "style.css"
        if not sp.exists() or force:
            sp.write_text(BASE_CSS, encoding="utf-8")
        jp = tdir / "site.js"
        if not jp.exists() or force:
            jp.write_text(SITE_JS, encoding="utf-8")

    # Cleanable universe = manifest entries whose raw HTML is on disk. Seed the
    # status so it keeps showing the download count and reports cleaning progress.
    on_disk = sum(1 for e in manifest if (raw_dir / e["local_path"]).exists())
    state.update(phase=CLEANING, target=only_target or "",
                 total=len(manifest), downloaded=on_disk,
                 clean_total=on_disk, cleaned=0)

    cleaned = 0
    index_pages: dict[str, list[tuple[str, str]]] = {}

    for entry in manifest:
        src = raw_dir / entry["local_path"]
        if not src.exists():
            continue
        dst = clean_dir / entry["local_path"]
        if dst.exists() and not force:
            index_pages.setdefault(entry["target"], []).append(
                (entry["local_path"], entry["original"]))
            state.incr(cleaned=1)  # already clean still counts toward progress
            continue
        try:
            html = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        this_clean = PurePosixPath(entry["local_path"])
        css_href = _relpath(this_clean, f"{entry['target']}/style.css")
        js_href = _relpath(this_clean, f"{entry['target']}/site.js")
        out = clean_html(html, entry["original"], this_clean, url_map, scope_hosts,
                         localize_by_target.get(entry["target"], False), ignore,
                         css_href, js_href)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(out, encoding="utf-8")
        cleaned += 1
        state.incr(cleaned=1)
        title = _title_of(html, entry["original"])
        index_pages.setdefault(entry["target"], []).append((entry["local_path"], title))

    for target_name, pages in index_pages.items():
        # Index links are relative to clean/, so include the target-prefixed paths.
        build_index(clean_dir, f"{target_name} docs", pages)
        # Also a per-target index inside the target folder.
        tdir = clean_dir / target_name
        if tdir.exists():
            rel_pages = [(str(PurePosixPath(p).relative_to(target_name)), t)
                         for p, t in pages]
            build_index(tdir, f"{target_name} docs", rel_pages)

    state.update(phase=DONE)
    print(f"[clean] cleaned {cleaned} files -> {clean_dir}")
    return cleaned


def watch(config: Config, state: State, only_target: str | None = None,
          interval: int = 60) -> None:
    print(f"[clean] watch mode, every {interval}s. Ctrl+C to stop.")
    while True:
        n = run(config, state, only_target)
        print(f"[clean] pass done — {n} files. waiting {interval}s...")
        time.sleep(interval)
