# wayback-py

A universal, pure-Python tool for scraping a website out of the Internet Archive
Wayback Machine and turning it into clean, self-contained offline HTML — with URL
filtering, resumable downloads, automatic rate-limit recovery, manifest-driven link
rewriting, and a live status command.

No Ruby, no `wayback_machine_downloader` gem — just Python and the CDX API.

## How it works

Three phases, all driven by `config.yaml` and sharing one canonical URL model:

1. **enumerate** — query the CDX API for every matching capture, deduplicate, apply
   include/exclude filters, and write `manifest.json` (the checklist of URLs + the
   snapshot to fetch + the local path each maps to).
2. **download** — fetch each URL's archived HTML into `raw/`. Resumable and
   rate-limit aware.
3. **clean** — strip Wayback artifacts, rewrite internal links to local paths using
   the manifest, link every page to one shared `style.css`, and write readable
   offline HTML into `clean/`.

Everything for a run lives under `run_dir`:

```
data/
  manifest.json      # the checklist (original URL -> snapshot -> local path)
  state.json         # live status (atomically updated)
  raw/<target>/...   # downloaded archived HTML
  clean/<target>/... # cleaned offline HTML + style.css + _index.html
```

## Install

```
pip install -r requirements.txt
```

(`gspread` + `google-auth` are only needed if you turn on Google Sheets reporting.)

## Usage

```
python -m wayback enumerate      --config config.yaml  # build manifest.json
python -m wayback download       --config config.yaml  # download (resumable)
python -m wayback clean          --config config.yaml  # build clean offline docs
python -m wayback fetch-external --config config.yaml  # fetch external assets/iframes
python -m wayback run            --config config.yaml  # all phases in sequence
python -m wayback status         --config config.yaml  # live status (separate terminal)
```

Windows shortcuts (double-click):

| Script | Does |
|---|---|
| `new.bat` | full run: enumerate → download → [fetch-external] → clean |
| `resume.bat` | resume the download |
| `fetch-external.bat` | fetch external-domain assets/iframes, then re-clean |
| `cleanup.bat` | clean only (`cleanup.bat --force` to re-clean everything) |
| `status.bat` | live status, refreshing every few seconds (`status.bat 5` = every 5s) |

### Useful flags

- `--target NAME` — limit any command to one target.
- `enumerate --max N` — cap URLs (smoke test).
- `enumerate --from YYYYMMDD --to YYYYMMDD` — override the target's date window.
- `enumerate --merge` — add only *new* URLs to an existing manifest, keeping your
  already-downloaded pages untouched (e.g. fold in an earlier year:
  `enumerate --merge --from 20060101 --to 20061231`).
- `clean --force` — re-clean every page (rebuilds `style.css` and indexes).
- `clean --watch` — re-clean every 60s while a download runs.
- `remap` — recompute local paths/keys on the existing manifest without touching CDX.

### Live status

```
Status    : alive
Process   : Downloading
Target    : devdocs
Downloaded: 412 / 1233
Cleaned   : 0 / 0
```

`Status` is `alive` when the running process is live, `idle` when finished, `dead`
if it crashed. `Process` is Enumerating / Downloading / Waiting for rate limit /
Fetching assets / Cleaning / Idle. `Downloaded X / Y` climbs during the download and
each fetch-external wave.

## Resuming after a rate limit or crash

- **Within a run:** HTTP 429/503 backs off and retries; transient gateway errors
  (502/504/52x) are retried. On a connection refusal/timeout (likely an IP block) a
  global gate pauses all workers, probes the CDX API until it responds, then resumes —
  no manual action.
- **Across restarts:** progress is on disk. Re-run `download` and it skips every file
  already present and continues from the manifest.

## Configuration (`config.yaml`)

```yaml
run_dir: "./data"
threads: 4
request_delay: 1.0
max_retries: 10
snapshot: first            # first | last | latest | closest:YYYYMMDD
                           # 'first' uses fast server-side collapse (earliest in range).
                           # last/latest/closest need an uncollapsed scan (slow on big
                           # sites); for "newest" cheaply, narrow `from` to a later year.

# Query params to ignore when keying/naming URLs, so variants that differ only by
# these collapse to one page (e.g. ?v=1.0&method=X == ?method=X).
ignore_query_params:
  - v

external_asset: false      # fetch resources pages pull from EXTERNAL domains
                           # (images, CSS, JS, SWF, XML, <iframe> content). See below.
external_exclude: []       # regexes; external URLs matching any are NOT fetched
                           # (e.g. 'facebook\.com/(plugins|connect)/' for social widgets)
kill_redirects: false      # strip page-driven redirects (meta-refresh + inline no-JS->JS
                           # bounce calls) so fetched widget pages display, don't redirect

google_sheets:             # optional; omit or enabled:false to skip
  enabled: false

targets:
  - name: devdocs
    url: "http://developers.facebook.com"
    match: prefix          # exact | prefix | host | domain
    from: "20070101"
    to: "20110930"
    html_only: true        # CDX filter mimetype:text/html
    include: []            # URL must match >=1 of these regexes (optional)
    exclude:               # URL dropped if it matches any of these regexes
      - 'action=(edit|history|raw)'
    localize_assets: false
```

### Link rewriting rules

For each `<a href>` on a cleaned page:

- **in scope and captured** → rewritten to a relative local path (`#fragment` kept).
- **in scope but never captured** → marked dead: `href="#"`, `data-broken="<url>"`,
  `class="dead"`.
- **out of scope** → left exactly as-is (a Wayback-wrapped link still works).

URLs are canonicalized first: `:80`/`:443` and a trailing dot on the host are folded
away, `//` in paths is collapsed, and `ignore_query_params` are dropped — so
equivalent URLs resolve to the same page.

### External assets (`external_asset: true`)

Archived pages routinely pull images, CSS, JS, SWF, XML and `<iframe>` content from
**external** domains (CDNs, widgets, partner sites). With `external_asset: true` the
tool fetches those from Wayback (period-correct) so the offline site is self-contained:

- A separate **`manifest_external.json`** records them (same shape as `manifest.json`
  plus a `kind` of `"asset"` or `"iframe"`); the main manifest stays untouched.
- Files land under **`clean/<target>/_external/<host>/…`**, never colliding with the
  site tree, and the cleaner rewrites page references to those local copies.
- An **`<iframe>`** is fetched as a *page plus its own direct assets* — its images/CSS/JS
  are pulled too, but its `<a>` links are **not** followed (no crawling the other site).
- Discovery runs in **waves** (a stylesheet/iframe is fetched, then scanned for what *it*
  references) until nothing new is found.

Run it automatically as part of `run`, or standalone after a download with
`python -m wayback fetch-external`. It is idempotent — already-fetched resources and
on-disk files are skipped. Note: "external" means *any host other than the target's*,
so sibling subdomains (e.g. `blog.example.com` for a `www.example.com` target) are
treated as external; widen the target's `match` to `domain` if you want them in the
main manifest instead.

**Skipping noise (`external_exclude`):** some third-party widgets (Facebook like-boxes,
social connect plugins) only redirect or break offline and pull in dozens of dead
assets. List regexes in `external_exclude` to skip fetching those URLs; the referencing
`<iframe>`/tag then keeps its original archive URL (a working online fallback) instead
of a broken local copy.

**Keeping redirecting pages (`kill_redirects`):** if you'd rather *display* a fetched
widget/iframe than skip it, set `kill_redirects: true`. The cleaner then strips
`<meta http-equiv="refresh">` tags and the inline no-JS→JS bounce calls (e.g. Facebook's
`redirectToJSPage`) from the page, so it renders its captured content instead of
navigating away. The injected `wayback-py-script.js` additionally no-ops
`location.assign/replace` at runtime. The one thing nothing can stop is a third-party
script setting `location.href = …` directly (a browser security boundary) — for those,
`external_exclude` is the fallback.

### Local file naming

- source extensions (`.php`, `.asp`, …) become `.html`
- a query string becomes nested `/key/value/` path segments
- a URL other URLs live underneath (or ends in `/`) becomes `<path>/index.html`
- collisions get a numeric suffix so distinct URLs never overwrite

## Styling & hosting

Each target folder is **self-contained**: the cleaner writes
`clean/<target>/wayback-py-style.css` and `clean/<target>/wayback-py-script.js` into the
target folder, and every page links to both with relative paths that stay inside it.

- **`wayback-py-style.css`** — edit this one file to restyle the whole target. For
  docs-style captures (`html_only: true`) it carries readable base styling; for full-site
  captures (`html_only: false`) it is written **empty** so the site's own CSS isn't
  overridden (still linked, so you can add tweaks).
- **`wayback-py-script.js`** — shared script loaded on every page. Edit it to change
  behaviour site-wide **without re-cleaning** (it runs in the browser). The default
  injects a responsive `<meta name="viewport">` and no-ops `location.assign/replace`
  redirects; add your own tweaks below the marked line.

Both are only (re)written when missing or on `clean --force`, so your edits survive a
normal incremental `clean`.

To publish the result, serve the **target folder** as the web root — e.g. point your
server at `clean/<target>/` (`python -m http.server` works). Its `index.html` is the
homepage and every link/stylesheet resolves within that folder, so it works as a
standalone static site. (Serve over HTTP, not `file://` — JS-driven pages need it.)

## Optional: Google Sheets status

Set `google_sheets.enabled: true` with a service-account JSON and spreadsheet id to
push throttled status rows to a sheet. Disabled by default; the dependency
(`gspread`) is only imported when enabled.
