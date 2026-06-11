"""CDX API client.

Wraps the Wayback CDX server with retry/backoff and resumeKey pagination, so
enumerate.py can stream every matching capture for a target without worrying about
rate limits or page-size caps.
"""

from __future__ import annotations

import time
from typing import Iterator

import requests

CDX_API = "https://web.archive.org/cdx/search/cdx"
HEADERS = {
    "User-Agent": "wayback-py/1.0 (+archival-research)",
}

# matchType expected by the CDX server for each config 'match' value.
MATCH_TYPES = {
    "exact": "exact",
    "prefix": "prefix",
    "host": "host",
    "domain": "domain",
}

# Page size per CDX request. Kept modest because the CDX server is often slow;
# smaller pages complete within the timeout, and resumeKey walks the rest.
PAGE_LIMIT = 1000
REQUEST_TIMEOUT = 120


class CDXError(Exception):
    pass


def ping(timeout: float = 30.0) -> bool:
    """Lightweight reachability check used by the downloader's block prober."""
    try:
        r = requests.get(
            CDX_API,
            params={"url": "example.com", "output": "json", "limit": "1"},
            headers=HEADERS,
            timeout=timeout,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def _request(params: list, max_retries: int) -> requests.Response:
    wait = 10
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(CDX_API, params=params, headers=HEADERS,
                             timeout=REQUEST_TIMEOUT)
            # Rate limiting (429) and transient gateway errors (502/503/504 and the
            # Cloudflare 52x family) are all retryable — the CDX server is frequently
            # overloaded and returns these under load.
            if r.status_code in (429, 502, 503, 504, 520, 522, 524):
                retry_after = r.headers.get("Retry-After")
                sleep = int(retry_after) if (retry_after or "").isdigit() else wait
                print(f"    [cdx] {r.status_code} (attempt {attempt}/{max_retries}), "
                      f"waiting {sleep}s...")
                time.sleep(sleep)
                wait = min(wait * 2, 300)
                continue
            if r.status_code == 400:
                # CDX returns 400 when a page index is past the end — treat as empty.
                raise _EndOfResults()
            r.raise_for_status()
            r.json()  # force-read the body so a truncated stream fails here, not later
            return r
        except _EndOfResults:
            raise
        except requests.exceptions.HTTPError as exc:
            # Non-retryable HTTP status (4xx other than 400/429/503).
            raise CDXError(f"CDX HTTP error: {exc}") from exc
        except (requests.exceptions.RequestException, ValueError) as exc:
            # Timeouts, connection resets, chunked-encoding drops, bad JSON — all
            # transient for the slow CDX server. Back off and retry.
            last_exc = exc
            print(f"    [cdx] {type(exc).__name__} (attempt {attempt}/{max_retries}), "
                  f"waiting {wait}s...")
            time.sleep(wait)
            wait = min(wait * 2, 300)
    raise CDXError(f"CDX request failed after {max_retries} attempts: {last_exc}")


class _EndOfResults(Exception):
    pass


def iter_captures(
    url: str,
    match: str = "prefix",
    *,
    from_ts: str = "",
    to_ts: str = "",
    html_only: bool = True,
    include_regex: str | None = None,
    fields: tuple[str, ...] = ("timestamp", "original", "statuscode", "mimetype", "digest"),
    collapse: str = "urlkey",
    max_retries: int = 10,
) -> Iterator[dict]:
    """Yield one dict per capture row, paginating with resumeKey.

    Server-side filters: statuscode:200, optional mimetype:text/html, optional
    original:<include_regex>, plus collapse to deduplicate by urlkey.
    """
    base_params = [
        ("url", url),
        ("matchType", MATCH_TYPES.get(match, "prefix")),
        ("output", "json"),
        ("fl", ",".join(fields)),
        ("filter", "statuscode:200"),
        ("limit", str(PAGE_LIMIT)),
        ("showResumeKey", "true"),
    ]
    if collapse:
        base_params.append(("collapse", collapse))
    if html_only:
        base_params.append(("filter", "mimetype:text/html"))
    if include_regex:
        base_params.append(("filter", f"original:{include_regex}"))
    if from_ts:
        base_params.append(("from", from_ts))
    if to_ts:
        base_params.append(("to", to_ts))

    resume_key: str | None = None
    while True:
        params = list(base_params)
        if resume_key:
            params.append(("resumeKey", resume_key))
        try:
            resp = _request(params, max_retries)
        except _EndOfResults:
            return
        rows = resp.json()
        if not rows:
            return

        header = rows[0]
        data = rows[1:]
        if not data:
            return

        # With showResumeKey, the resume key arrives as the last row preceded by a
        # blank row. Detect and strip it.
        resume_key = None
        if len(data) >= 2 and data[-2] == [] and len(data[-1]) == 1:
            resume_key = data[-1][0]
            data = data[:-2]

        for row in data:
            if not row or len(row) != len(header):
                continue
            yield dict(zip(header, row))

        if not resume_key:
            return
