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
python -m wayback enumerate --config config.yaml     # build manifest.json
python -m wayback download  --config config.yaml     # download (resumable)
python -m wayback clean     --config config.yaml     # build clean offline docs
python -m wayback run       --config config.yaml     # all three in sequence
python -m wayback status    --config config.yaml     # live status (separate terminal)
```

Windows shortcuts (double-click):

| Script | Does |
|---|---|
| `new.bat` | full run: enumerate → download → clean |
| `resume.bat` | resume the download |
| `cleanup.bat` | clean only (`cleanup.bat --force` to re-clean everything) |
| `status.bat` | live status, refreshing every few seconds |

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
Cleaning / Idle.

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

### Local file naming

- source extensions (`.php`, `.asp`, …) become `.html`
- a query string becomes nested `/key/value/` path segments
- a URL other URLs live underneath (or ends in `/`) becomes `<path>/index.html`
- collisions get a numeric suffix so distinct URLs never overwrite

## Styling

Every cleaned page links to one shared `clean/style.css`. Edit that single file to
restyle the whole archive. `clean --force` regenerates it from the default theme.

## Optional: Google Sheets status

Set `google_sheets.enabled: true` with a service-account JSON and spreadsheet id to
push throttled status rows to a sheet. Disabled by default; the dependency
(`gspread`) is only imported when enabled.
