"""Phase 1: build manifest.json — the deduplicated checklist of URLs to download.

Queries CDX for every matching capture, applies include/exclude filters, picks one
snapshot per URL according to the configured strategy, and maps each to a local path.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import cdx
from .config import Config, Target
from .state import State, ENUMERATING
from .urls import _dedupe, assign_local_paths, normalize

MANIFEST_FILENAME = "manifest.json"
EXTERNAL_MANIFEST_FILENAME = "manifest_external.json"


def manifest_path(run_dir: Path) -> Path:
    return Path(run_dir) / MANIFEST_FILENAME


def external_manifest_path(run_dir: Path) -> Path:
    return Path(run_dir) / EXTERNAL_MANIFEST_FILENAME


def load_external_manifest(run_dir: Path) -> list[dict]:
    """Return the external-asset manifest, or [] when it doesn't exist yet.

    Unlike load_manifest this never raises — external assets are optional, so the
    absence of the file simply means none have been fetched.
    """
    path = external_manifest_path(run_dir)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _passes_filters(original: str, target: Target) -> bool:
    if target.include_re and not any(r.search(original) for r in target.include_re):
        return False
    if any(r.search(original) for r in target.exclude_re):
        return False
    return True


def _select(rows: list[dict], snapshot: str) -> dict:
    """Pick one capture row from several sharing a urlkey, per snapshot strategy."""
    if snapshot == "first":
        return min(rows, key=lambda r: r["timestamp"])
    if snapshot in ("last", "latest"):
        return max(rows, key=lambda r: r["timestamp"])
    # closest:YYYYMMDD
    target_ts = snapshot.split(":", 1)[1]
    return min(rows, key=lambda r: abs(int(r["timestamp"]) - int(target_ts.ljust(14, "0"))))


def enumerate_target(target: Target, config: Config, state: State | None = None,
                     from_ts: str | None = None, to_ts: str | None = None) -> list[dict]:
    """Return manifest entries for one target. from_ts/to_ts override the target window."""
    # Push the include regex server-side only when there is exactly one (CDX takes one).
    server_include = (
        target.include[0] if len(target.include) == 1 else None
    )
    ignore = tuple(config.ignore_query_params)
    from_ts = from_ts or target.from_ts
    to_ts = to_ts or target.to_ts
    # 'first' uses cheap server-side collapse (earliest per URL). last/latest/closest
    # need every capture, so collapse must be off (slow on large sites).
    collapse = "urlkey" if config.snapshot == "first" else ""

    groups: dict[str, list[dict]] = {}
    seen = 0
    for row in cdx.iter_captures(
        target.url,
        target.match,
        from_ts=from_ts,
        to_ts=to_ts,
        html_only=target.html_only,
        include_regex=server_include,
        fields=("urlkey", "timestamp", "original", "statuscode", "mimetype", "digest"),
        collapse=collapse,
        max_retries=config.max_retries,
    ):
        original = row.get("original", "")
        if not original or not _passes_filters(original, target):
            continue
        groups.setdefault(row["urlkey"], []).append(row)
        seen += 1
        if state and seen % 500 == 0:
            state.update(phase=ENUMERATING, target=target.name, total=len(groups))

    chosen_rows = [_select(rows, config.snapshot) for rows in groups.values()]

    # Second dedup pass on the canonical key (after dropping ignore_params), so URLs
    # that differ only by an ignored param (e.g. ?v=1.0) collapse to one entry.
    canonical: dict[str, dict] = {}
    for r in chosen_rows:
        ck = normalize(r["original"], ignore)
        canonical[ck] = r if ck not in canonical else _select([canonical[ck], r],
                                                               config.snapshot)
    chosen_rows = list(canonical.values())

    originals = [r["original"] for r in chosen_rows]
    # Batch path assignment so directory-index detection sees the whole URL set.
    path_map = assign_local_paths(originals, prefix=target.name, ignore_params=ignore)

    entries = []
    for chosen in chosen_rows:
        original = chosen["original"]
        entries.append({
            "original": original,
            "key": normalize(original, ignore),
            "timestamp": chosen["timestamp"],
            "wayback_url": f"https://web.archive.org/web/{chosen['timestamp']}/{original}",
            "local_path": path_map[original],
            "target": target.name,
        })
    entries.sort(key=lambda e: e["key"])
    return entries


def run(config: Config, state: State, only_target: str | None = None,
        max_urls: int | None = None, merge: bool = False,
        from_ts: str | None = None, to_ts: str | None = None) -> list[dict]:
    targets = [config.target(only_target)] if only_target else config.targets
    state.update(phase=ENUMERATING, downloaded=0, failed=0, total=0)

    all_entries: list[dict] = []
    for target in targets:
        win_from = from_ts or target.from_ts
        win_to = to_ts or target.to_ts
        print(f"[enumerate] {target.name}: querying CDX for {target.url} "
              f"({target.match}, {win_from}-{win_to})...")
        entries = enumerate_target(target, config, state, from_ts=from_ts, to_ts=to_ts)
        print(f"[enumerate] {target.name}: {len(entries)} unique URLs")
        all_entries.extend(entries)
        state.update(phase=ENUMERATING, target=target.name, total=len(all_entries))

    if max_urls is not None:
        all_entries = all_entries[:max_urls]
        print(f"[enumerate] capped to {len(all_entries)} URLs (--max)")

    path = manifest_path(config.run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    if merge and path.exists():
        all_entries = _merge_into_existing(all_entries, config)

    path.write_text(json.dumps(all_entries, indent=2), encoding="utf-8")
    state.update(phase=ENUMERATING, total=len(all_entries))
    print(f"[enumerate] wrote {len(all_entries)} entries -> {path}")
    return all_entries


def _merge_into_existing(new_entries: list[dict], config: Config) -> list[dict]:
    """Add only URLs not already in the manifest; keep existing entries untouched.

    Preserves already-downloaded pages (their timestamps, paths, files) and just
    appends newly-discovered URLs — e.g. pages whose only good capture predates the
    original window. New local paths are de-duplicated against existing ones.
    """
    ignore = tuple(config.ignore_query_params)
    existing = load_manifest(config.run_dir)
    existing_keys = {normalize(e["original"], ignore) for e in existing}
    used_paths = {e["local_path"] for e in existing}

    additions = []
    for e in new_entries:
        if e["key"] in existing_keys:
            continue
        e["local_path"] = _dedupe(e["local_path"], used_paths)
        used_paths.add(e["local_path"])
        existing_keys.add(e["key"])
        additions.append(e)

    print(f"[enumerate] merge: kept {len(existing)} existing, added {len(additions)} new")
    return existing + additions


def load_manifest(run_dir: Path) -> list[dict]:
    path = manifest_path(run_dir)
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}. Run `enumerate` first.")
    return json.loads(path.read_text(encoding="utf-8"))


def remap(config: Config, state: State) -> int:
    """Recompute local_path for every manifest entry using the current naming rules.

    Pure local operation — does not touch CDX. Lets the on-disk layout be changed
    after the (slow) enumeration without rebuilding the URL list.
    """
    manifest = load_manifest(config.run_dir)
    ignore = tuple(config.ignore_query_params)
    by_target: dict[str, list[dict]] = {}
    for e in manifest:
        by_target.setdefault(e["target"], []).append(e)

    for target_name, entries in by_target.items():
        originals = [e["original"] for e in entries]
        path_map = assign_local_paths(originals, prefix=target_name, ignore_params=ignore)
        for e in entries:
            e["local_path"] = path_map[e["original"]]
            e["key"] = normalize(e["original"], ignore)
            e["key"] = normalize(e["original"])  # refresh in case normalize changed

    manifest_path(config.run_dir).write_text(json.dumps(manifest, indent=2),
                                             encoding="utf-8")
    print(f"[remap] rewrote local_path for {len(manifest)} entries")
    return len(manifest)
