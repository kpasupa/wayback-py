"""Canonical URL handling shared by every phase.

Holds the single normalization function, the URL->local-path mapping (so manifest
keys and on-disk paths always agree), Wayback-prefix unwrapping, and fragment
handling. Local paths are assigned in a batch over the whole manifest so that
directory-index detection (a URL that other URLs live underneath) works.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, unquote

# Absolute Wayback URL:   https://web.archive.org/web/TIMESTAMP[flags]/http://original...
WAYBACK_ABS_RE = re.compile(
    r"https?://web\.archive\.org/web/\d+[a-z_]*/(https?://.+)$", re.I
)
# Relative Wayback URL injected into pages: /web/TIMESTAMP[flags]/http://original...
WAYBACK_REL_RE = re.compile(r"/web/\d+[a-z_]*/(https?://.+)$", re.I)

# Server-side script extensions we rewrite to .html on disk.
SOURCE_EXTS = {
    "php", "php3", "php4", "php5", "phtml", "asp", "aspx", "jsp", "jspx",
    "cgi", "pl", "do", "action", "cfm", "htm", "html", "shtml", "xhtml",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._()-]")


# ---------------------------------------------------------------------------
# Normalization / unwrapping / fragments
# ---------------------------------------------------------------------------

def _clean_host(netloc: str) -> str:
    host = netloc.lower().strip()
    if host.endswith(":80"):
        host = host[:-3]
    elif host.endswith(":443"):
        host = host[:-4]
    if host.endswith("."):
        host = host[:-1]
    return host


def _filter_query(query: str, ignore_params: tuple[str, ...]) -> str:
    """Return the query string with ignored params removed (order preserved)."""
    if not query:
        return ""
    if not ignore_params:
        return f"?{query}"
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if k not in ignore_params]
    return f"?{urlencode(kept)}" if kept else ""


def normalize(url: str, ignore_params: tuple[str, ...] = ()) -> str:
    """Canonical key for a URL: lowercase scheme+host, drop fragment, keep path+query.

    Port :80/:443 and a trailing dot on the host are folded away, // in the path is
    collapsed, trailing slashes on non-root paths are stripped, and any query
    parameters in `ignore_params` are removed — so equivalent URLs collapse to one key.
    """
    base, _ = split_fragment(url.strip())
    p = urlparse(base)
    scheme = (p.scheme or "http").lower()
    host = _clean_host(p.netloc)
    path = unquote(p.path) or "/"
    path = re.sub(r"/{2,}", "/", path)  # collapse // (CDX captures are full of them)
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return f"{scheme}://{host}{path}{_filter_query(p.query, ignore_params)}"


def split_fragment(href: str) -> tuple[str, str]:
    """Split an href into (base, fragment_including_hash). Fragment is '' if none."""
    if "#" in href:
        i = href.index("#")
        return href[:i], href[i:]
    return href, ""


def unwrap_wayback(href: str) -> str | None:
    """Return the original URL embedded in a Wayback href, else None."""
    if not href:
        return None
    m = WAYBACK_ABS_RE.match(href) or WAYBACK_REL_RE.match(href)
    return m.group(1) if m else None


def resolve_href(href: str, page_original_url: str) -> str | None:
    """Resolve an href on an archived page to the original absolute URL it targets.

    Handles Wayback-wrapped, absolute, and relative hrefs. Returns None for
    anchors, mailto:, javascript:, etc. The fragment is preserved on the result.
    """
    if not href:
        return None
    low = href.strip().lower()
    if low.startswith(("#", "mailto:", "javascript:", "tel:", "data:")):
        return None
    base, frag = split_fragment(href)
    unwrapped = unwrap_wayback(base)
    if unwrapped:
        return unwrapped + frag
    if base.lower().startswith(("http://", "https://")):
        return base + frag
    return urljoin(page_original_url, base) + frag


def resolve_css_ref(ref: str, css_url: str | None) -> str | None:
    """Resolve a CSS url()/@import target to the absolute original URL it points at.

    Handles Wayback-wrapped, absolute, protocol-relative (//host/...), and relative
    refs (resolved against the stylesheet's own original URL). Returns None for data:
    URIs and empties.
    """
    ref = ref.strip().strip("'\"").strip()
    if not ref or ref.lower().startswith("data:"):
        return None
    unwrapped = unwrap_wayback(ref)
    if unwrapped:
        return unwrapped
    if ref.lower().startswith(("http://", "https://")):
        return ref
    if ref.startswith("//"):
        return "http:" + ref
    return urljoin(css_url, ref) if css_url else None


def host_of(url: str) -> str:
    return _clean_host(urlparse(url).netloc)


def registrable_suffix_match(host: str, base_host: str) -> bool:
    """True if host == base_host or is a subdomain of base_host."""
    host, base_host = _clean_host(host), _clean_host(base_host)
    return host == base_host or host.endswith("." + base_host)


# ---------------------------------------------------------------------------
# Local-path assignment
# ---------------------------------------------------------------------------

def _safe(seg: str) -> str:
    seg = _UNSAFE.sub("_", unquote(seg))
    if seg in ("", ".", ".."):
        return "_"
    return seg[:150]


def _stem_segments(path: str) -> list[str]:
    """Path segments with source extensions stripped from every segment."""
    result = []
    for seg in [_safe(s) for s in path.split("/") if s]:
        if "." in seg:
            stem, ext = seg.rsplit(".", 1)
            if ext.lower() in SOURCE_EXTS and stem:
                seg = stem
        result.append(seg)
    return result


def _has_asset_ext(seg: str) -> bool:
    """True if segment ends with a non-source extension (e.g. .png, .css, .js)."""
    if "." not in seg:
        return False
    return seg.rsplit(".", 1)[1].lower() not in SOURCE_EXTS


def _query_segments(query: str) -> list[str]:
    """Turn a query string into path segments: 'a=1&b=2' -> ['a','1','b','2']."""
    out: list[str] = []
    for token in query.split("&"):
        if not token:
            continue
        if "=" in token:
            k, v = token.split("=", 1)
            out.append(_safe(k))
            if v:
                out.append(_safe(v))
        else:
            out.append(_safe(token))
    return [s for s in out if s]


def assign_local_paths(originals: list[str], prefix: str = "",
                       ignore_params: tuple[str, ...] = ()) -> dict[str, str]:
    """Map each original URL -> a POSIX local path (optionally under `prefix`).

    Rules:
      * source extensions (.php/.asp/...) become .html
      * a query string becomes nested /key/value/ segments under the page stem
        (params in `ignore_params` are dropped first)
      * a URL that other URLs live underneath (or ends in '/', or has query
        children) becomes <stem>/index.html
      * filename collisions get a numeric suffix so distinct URLs never overwrite
    """
    parsed: dict[str, dict] = {}
    for url in originals:
        base, _ = split_fragment(url)
        p = urlparse(base)
        stem = _stem_segments(unquote(p.path))
        query = _filter_query(p.query, ignore_params).lstrip("?")
        parsed[url] = {
            "stem": stem,
            "stem_key": "/".join(stem),
            "query": _query_segments(query),
            "trailing_slash": p.path.endswith("/") and p.path != "/",
        }

    # Determine which stem keys are directories.
    dirs: set[str] = set()
    for info in parsed.values():
        stem = info["stem"]
        if info["query"]:
            dirs.add(info["stem_key"])  # has query children
        if info["trailing_slash"]:
            dirs.add(info["stem_key"])
        for i in range(1, len(stem)):  # every ancestor is a directory
            dirs.add("/".join(stem[:i]))

    result: dict[str, str] = {}
    used: set[str] = set()
    for url, info in parsed.items():
        stem, qsegs = info["stem"], info["query"]
        if qsegs:
            parts = stem + qsegs
            path_segs = parts[:-1] + [parts[-1] + ".html"]
        elif not stem:
            path_segs = ["index.html"]
        elif info["stem_key"] in dirs:
            path_segs = stem + ["index.html"]
        elif _has_asset_ext(stem[-1]):
            path_segs = stem  # preserve original extension (.png, .css, .js, etc.)
        else:
            path_segs = stem[:-1] + [stem[-1] + ".html"]

        rel = "/".join(([prefix] if prefix else []) + path_segs)
        rel = _dedupe(rel, used)
        used.add(rel)
        result[url] = rel
    return result


def _dedupe(path: str, used: set[str]) -> str:
    if path not in used:
        return path
    if path.endswith(".html"):
        stem, ext = path[:-5], ".html"
    else:
        stem, ext = path, ""
    n = 2
    while f"{stem}_{n}{ext}" in used:
        n += 1
    return f"{stem}_{n}{ext}"


def url_to_local_path(url: str, prefix: str = "") -> str:
    """Single-URL convenience wrapper (no directory-index detection)."""
    return assign_local_paths([url], prefix)[url]
