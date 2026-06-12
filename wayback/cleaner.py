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
from .enumerate import load_external_manifest, load_manifest
from .state import State, CLEANING, DONE
from .urls import (host_of, normalize, registrable_suffix_match, resolve_css_ref,
                   resolve_href, split_fragment, unwrap_wayback)

STYLE_FILE = "wayback-py-style.css"
SCRIPT_FILE = "wayback-py-script.js"

_BINARY_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".ico", ".bmp", ".webp",
    ".swf", ".mp3", ".mp4", ".ogg", ".wav", ".flv",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip",
}


def _is_binary(data: bytes, local_path: str) -> bool:
    """True if the file should be copied as-is without HTML/CSS processing."""
    if Path(local_path).suffix.lower() in _BINARY_EXTS:
        return True
    return b"\x00" in data[:512]


def _sniff_css(data: bytes) -> bool:
    """True if raw bytes look like CSS rather than HTML or JavaScript.

    Catches PHP/ASP scripts that output CSS but got mapped to .html by
    assign_local_paths (which strips source extensions).
    """
    head = data[:500].lstrip()
    if head[:5].lower() in (b"<!doc", b"<html", b"<?xml"):
        return False
    if head[:2] in (b"<!", b"<?"):
        return False
    # Skip a leading block comment (/* ... */) — both CSS and JS use these.
    check = head
    if check.startswith(b"/*"):
        end = check.find(b"*/")
        if end >= 0:
            check = check[end + 2:].lstrip()
    # Reject JavaScript: tokens that appear right after any leading comment.
    for marker in _JS_MARKERS:
        if check.startswith(marker):
            return False
    # CSS must have both { and : (property blocks).
    return b"{" in data[:4000] and b":" in data[:4000]


_JS_MARKERS = (
    b"(function", b"!function", b";(function", b"function(", b"function ",
    b"var ", b"let ", b"const ", b"window.", b"document.", b"jQuery", b"$.fn",
    b"$(", b"(window", b"(this", b'"use strict"', b"'use strict'", b"/*!",
)


def _sniff_js(data: bytes) -> bool:
    """True if raw bytes look like JavaScript rather than HTML or CSS.

    Catches assets whose URL had a query string (e.g. script.js?ver=1.0), which
    assign_local_paths names <stem>/ver/1.0.html — so without this they'd be cleaned
    as HTML and the injected <link>/<script> would corrupt the script.
    """
    head = data[:600].lstrip().lstrip(b"\xef\xbb\xbf")  # drop leading BOM/whitespace
    if head[:1] in (b"<",):
        return False  # HTML/XML markup
    if head.startswith(b"/*"):  # strip a leading block comment (license banner)
        end = head.find(b"*/")
        if end >= 0:
            head = head[end + 2:].lstrip()
    if head.startswith(b"//"):  # strip a leading line comment
        nl = head.find(b"\n")
        head = head[nl + 1:].lstrip() if nl >= 0 else b""
    return any(head.startswith(m) for m in _JS_MARKERS)

# Wayback toolbar element ids.
WAYBACK_IDS = {
    "wm-ipp", "wm-ipp-base", "wm-ipp-print", "donato", "playback",
    "wm-share", "wm-tabs", "wm-toolbar",
}
# Wayback infrastructure hosts (toolbar CSS/JS). Only these get stripped from
# link/script src — original site assets wrapped in web.archive.org/web/... are kept.
WAYBACK_INFRA_RE = re.compile(r"web-static\.archive\.org", re.I)
# Wayback script signatures found in inline content.
WAYBACK_CONTENT_RE = re.compile(r"__wm\.|wombat|WB_wombat|RufflePlayer", re.I)
DEAD_LOCAL_RE = re.compile(r"^/(skins|extensions|opensearch_desc|favicon\.ico)")

# Inline-script redirect patterns removed when kill_redirects is on. These are the
# no-JS -> JS bounce calls some sites (e.g. Facebook social widgets) queue in the page
# itself; deleting the call kills the redirect while the static content still renders.
_INLINE_REDIRECT_RES = (
    re.compile(r',?\s*\["NoscriptOverride","redirectToJSPage",\[\],\[[^\]]*\]\]'),
)


def _kill_redirects(soup: BeautifulSoup) -> None:
    """Neutralize page-driven redirects: <meta refresh> tags and inline redirect calls.

    Cannot stop a third-party script's `location.href = …` (browsers forbid intercepting
    the location setter), but removing the inline call that *invokes* the redirect — and
    any meta-refresh — handles the common archive cases without breaking page content.
    """
    for m in soup.find_all("meta"):
        if (m.get("http-equiv") or "").lower() == "refresh":
            m.decompose()
    for sc in soup.find_all("script"):
        if sc.get("src") or not sc.string:
            continue
        text = sc.string
        if "redirectToJSPage" not in text:
            continue
        for rx in _INLINE_REDIRECT_RES:
            text = rx.sub("", text)
        sc.string = text

# Rewrite url() in CSS files.
# Matches url(...) with any target — absolute, Wayback-wrapped, or relative. The ref
# is classified/resolved by resolve_css_ref (data: URIs are skipped there).
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^)'"]+?)['"]?\s*\)""", re.I)

BASE_CSS = """/* wayback-py-style.css — edit this file to restyle every page. */
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

# Used instead of BASE_CSS for targets captured WITH their own assets (html_only:
# false): the site brings its own stylesheets, so our base styling would fight the
# original layout. The file is still injected/linked, just empty for you to edit.
EMPTY_CSS = """/* wayback-py-style.css — intentionally empty.
   This target was captured with its own CSS (html_only: false), so no base styling is
   applied. Add site-wide tweaks here; this file is linked on every page in this folder. */
"""

SITE_JS = """/* wayback-py-script.js - shared script for every page in this folder.
   Edit this one file to affect the whole site (no re-clean needed). */
(function () {
  /* Responsive viewport (added at runtime so it works on every page). */
  if (!document.querySelector('meta[name="viewport"]')) {
    var m = document.createElement('meta');
    m.name = 'viewport';
    m.content = 'width=device-width, initial-scale=1.0';
    document.head.appendChild(m);
  }

  /* Keep archived pages from redirecting away (e.g. Facebook widgets, meta-refresh).
     Note: location.href/= redirects can't be intercepted by a page script. */
  function killMetaRefresh() {
    var metas = document.querySelectorAll('meta[http-equiv]');
    for (var i = 0; i < metas.length; i++) {
      if (/refresh/i.test(metas[i].getAttribute('http-equiv') || '')) {
        metas[i].parentNode.removeChild(metas[i]);
      }
    }
  }
  killMetaRefresh();
  document.addEventListener('DOMContentLoaded', killMetaRefresh);
  try { window.location.assign  = function () {}; } catch (e) {}
  try { window.location.replace = function () {}; } catch (e) {}

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
        src = tag.get("src") or tag.get("href") or ""
        content = tag.string or ""
        if (WAYBACK_INFRA_RE.search(src)
                or WAYBACK_CONTENT_RE.search(content)
                or WAYBACK_CONTENT_RE.search(src)
                or DEAD_LOCAL_RE.search(src)):
            tag.decompose()
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if "archive.org" in c or "FILE ARCHIVED" in c or "WAYBACK" in c.upper():
            c.extract()


def _rewrite_attr(tag, attr: str, page_original: str, this_clean: PurePosixPath,
                  url_map: dict[str, str], ignore_params: tuple[str, ...]) -> None:
    val = tag[attr]
    resolved = resolve_href(val, page_original)
    if not resolved:
        return
    base, frag = split_fragment(resolved)
    target_clean = url_map.get(normalize(base, ignore_params))
    if target_clean:
        tag[attr] = _relpath(this_clean, target_clean) + frag
    elif val.startswith("/web/"):
        # Root-relative Wayback URL not in manifest — make absolute so it resolves online.
        tag[attr] = "https://web.archive.org" + val


_LOCALIZABLE_LINK_RELS = {"stylesheet", "icon", "apple-touch-icon"}
_LAZY_ATTRS = ("data-src", "data-original", "data-lazy-src")

def _rewrite_assets(soup: BeautifulSoup, page_original: str, this_clean: PurePosixPath,
                    url_map: dict[str, str], ignore_params: tuple[str, ...]) -> None:
    """Rewrite img/script/iframe/stylesheet/favicon src attributes to local paths."""
    for tag in soup.find_all(["img", "script", "source", "video", "audio", "iframe",
                              "embed"], src=True):
        _rewrite_attr(tag, "src", page_original, this_clean, url_map, ignore_params)
    for tag in soup.find_all("object", attrs={"data": True}):
        _rewrite_attr(tag, "data", page_original, this_clean, url_map, ignore_params)
    for tag in soup.find_all("link", href=True):
        rels = set(tag.get("rel") or [])
        if rels & _LOCALIZABLE_LINK_RELS:
            _rewrite_attr(tag, "href", page_original, this_clean, url_map, ignore_params)
    # Lazy-load attributes (data-src etc.) used by JS slideshow/image libraries.
    for attr in _LAZY_ATTRS:
        for tag in soup.find_all(attrs={attr: True}):
            _rewrite_attr(tag, attr, page_original, this_clean, url_map, ignore_params)
    # Rewrite url() inside <style> blocks and inline style="" attributes (relative
    # refs resolve against the page's own URL).
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            style_tag.string = _rewrite_css_urls(style_tag.string, this_clean,
                                                 url_map, ignore_params, page_original)
    for tag in soup.find_all(style=True):
        tag["style"] = _rewrite_css_urls(tag["style"], this_clean, url_map,
                                         ignore_params, page_original)


def _rewrite_links(soup: BeautifulSoup, page_original: str, this_clean: PurePosixPath,
                   url_map: dict[str, str], scope_hosts: set[str],
                   ignore_params: tuple[str, ...]) -> None:
    for a in soup.find_all("a", href=True):
        resolved = resolve_href(a["href"], page_original)
        if not resolved:
            continue
        base, frag = split_fragment(resolved)
        target_clean = url_map.get(normalize(base, ignore_params))
        if target_clean:
            a["href"] = _relpath(this_clean, target_clean) + frag
            continue
        host = host_of(base)
        if any(registrable_suffix_match(host, h) for h in scope_hosts):
            a["data-broken"] = base
            a["href"] = "#"
            existing = a.get("class", [])
            a["class"] = existing + ["dead"] if existing else ["dead"]


def _rewrite_css_urls(css: str, this_clean: PurePosixPath,
                      url_map: dict[str, str], ignore_params: tuple[str, ...],
                      css_original: str | None = None) -> str:
    """Rewrite url() in CSS text to local relative paths for any asset in the manifest.

    Relative refs (e.g. url(../img/x.png)) are resolved against css_original — the
    stylesheet's own URL — so sprites/preloaders referenced relatively are localized too.
    """
    def _replace(m: re.Match) -> str:
        original = resolve_css_ref(m.group(1), css_original)
        if not original:
            return m.group(0)
        base, frag = split_fragment(original)
        target_clean = url_map.get(normalize(base, ignore_params))
        if target_clean:
            return f'url("{_relpath(this_clean, target_clean)}{frag}")'
        return m.group(0)
    return CSS_URL_RE.sub(_replace, css)


def clean_html(html: str, page_original: str, this_clean: PurePosixPath,
               url_map: dict[str, str], scope_hosts: set[str], localize: bool,
               ignore_params: tuple[str, ...], css_href: str, js_href: str,
               kill_redirects: bool = False) -> str:
    soup = BeautifulSoup(html, "html.parser")
    _strip_wayback(soup)
    if kill_redirects:
        _kill_redirects(soup)
    _rewrite_links(soup, page_original, this_clean, url_map, scope_hosts, ignore_params)
    if localize:
        _rewrite_assets(soup, page_original, this_clean, url_map, ignore_params)

    head = soup.find("head")
    if not head:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)
    for base in soup.find_all("base"):
        base.decompose()
    # wayback-py base stylesheet injected FIRST so original site stylesheets take precedence.
    link = soup.new_tag("link", rel="stylesheet", href=css_href)
    head.insert(0, link)
    # Shared per-target script injected last.
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
    # The external-asset manifest (if any) is folded in so external resources and
    # iframe pages are cleaned/copied and their references resolve via url_map.
    manifest = load_manifest(config.run_dir) + load_external_manifest(config.run_dir)
    if only_target:
        manifest = [e for e in manifest if e["target"] == only_target]

    raw_dir = Path(config.run_dir) / "raw"
    clean_dir = Path(config.run_dir) / "clean"
    ignore = tuple(config.ignore_query_params)

    # Files named .html by the path logic but whose bytes are really CSS or JS — e.g.
    # PHP that outputs CSS, or script.js?ver=1.0 named .../ver/1.0.html. Override their
    # extension so url_map/links use it and they are copied, not cleaned as HTML (which
    # would inject <link>/<script> and corrupt them).
    css_overrides: dict[str, str] = {}  # old .html local_path -> new .css/.js local_path
    for entry in manifest:
        lp = entry["local_path"]
        if not lp.endswith(".html"):
            continue
        src = raw_dir / lp
        if not src.exists():
            continue
        try:
            data = src.read_bytes()
        except OSError:
            continue
        if _is_binary(data, lp):
            continue
        if _sniff_css(data):
            css_overrides[lp] = lp[:-5] + ".css"
        elif _sniff_js(data):
            css_overrides[lp] = lp[:-5] + ".js"

    # An external resource that failed to download (e.g. a CDN image the Archive never
    # captured) is left out of the map, so its references stay as the absolute Wayback
    # URL (an online fallback) instead of becoming a dead local link.
    url_map = {
        normalize(e["original"], ignore): css_overrides.get(e["local_path"], e["local_path"])
        for e in manifest
        if "kind" not in e or (raw_dir / e["local_path"]).exists()
    }
    scope_hosts = {host_of(t.url) for t in config.targets}
    localize_by_target = {t.name: t.localize_assets for t in config.targets}
    html_only_by_target = {t.name: t.html_only for t in config.targets}

    clean_dir.mkdir(parents=True, exist_ok=True)
    for tname in {e["target"] for e in manifest}:
        tdir = clean_dir / tname
        tdir.mkdir(parents=True, exist_ok=True)
        # Docs-style captures (html_only) get our readable base styling; full-site
        # captures (html_only: false) keep their own CSS, so we inject an empty file.
        base_style = BASE_CSS if html_only_by_target.get(tname, True) else EMPTY_CSS
        sp = tdir / STYLE_FILE
        if not sp.exists() or force:
            sp.write_text(base_style, encoding="utf-8")
        jp = tdir / SCRIPT_FILE
        if not jp.exists() or force:
            jp.write_text(SITE_JS, encoding="utf-8")

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
        effective_lp = css_overrides.get(entry["local_path"], entry["local_path"])
        dst = clean_dir / effective_lp
        if dst.exists() and not force:
            index_pages.setdefault(entry["target"], []).append(
                (effective_lp, entry["original"]))
            state.incr(cleaned=1)
            continue
        try:
            raw_bytes = src.read_bytes()
        except OSError:
            continue

        this_clean = PurePosixPath(effective_lp)
        dst.parent.mkdir(parents=True, exist_ok=True)

        # If this entry is being written under an overridden extension (.css/.js), drop
        # the stale .html file a previous run may have left at the original path.
        if effective_lp != entry["local_path"]:
            stale = clean_dir / entry["local_path"]
            if stale.exists():
                stale.unlink()

        # Binary files (images, fonts, …) are copied straight through.
        if _is_binary(raw_bytes, effective_lp):
            dst.write_bytes(raw_bytes)
            cleaned += 1
            state.incr(cleaned=1)
            index_pages.setdefault(entry["target"], []).append(
                (effective_lp, entry["original"]))
            continue

        # CSS (incl. PHP-that-outputs-CSS) gets url() rewriting; HTML pages get full
        # cleaning; any other text resource (.js, .xml, ...) is copied through as-is so
        # injecting <link>/<script> can't corrupt it.
        if effective_lp.endswith(".css"):
            raw = raw_bytes.decode("utf-8", errors="replace")
            dst.write_text(_rewrite_css_urls(raw, this_clean, url_map, ignore,
                                             entry["original"]),
                           encoding="utf-8")
            title = entry["original"]
        elif effective_lp.endswith(".html"):
            raw = raw_bytes.decode("utf-8", errors="replace")
            css_href = _relpath(this_clean, f"{entry['target']}/{STYLE_FILE}")
            js_href = _relpath(this_clean, f"{entry['target']}/{SCRIPT_FILE}")
            localize = (localize_by_target.get(entry["target"], False)
                        or config.external_asset)
            out = clean_html(raw, entry["original"], this_clean, url_map, scope_hosts,
                             localize, ignore, css_href, js_href,
                             config.kill_redirects)
            dst.write_text(out, encoding="utf-8")
            title = _title_of(raw, entry["original"])
        else:
            dst.write_bytes(raw_bytes)
            title = entry["original"]

        cleaned += 1
        state.incr(cleaned=1)
        index_pages.setdefault(entry["target"], []).append((effective_lp, title))

    for target_name, pages in index_pages.items():
        build_index(clean_dir, f"{target_name} docs", pages)
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
