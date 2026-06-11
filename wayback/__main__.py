"""CLI entry point: python -m wayback <command>.

Commands:
  enumerate   build manifest.json (the checklist) from CDX
  download    download manifest URLs to raw/ (resumable, rate-limit aware)
  clean       turn raw/ HTML into clean offline docs in clean/
  run         enumerate -> download -> clean
  status      print live status from state.json (run in a separate terminal)
"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config
from .state import PHASE_LABELS, liveness, read_state


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--target", default=None, help="limit to one target by name")


def cmd_enumerate(args) -> int:
    from . import enumerate as enum
    from .reporter import make_state
    config = load_config(args.config)
    state = make_state(config)
    enum.run(config, state, only_target=args.target, max_urls=args.max,
             merge=args.merge, from_ts=args.from_ts, to_ts=args.to_ts)
    return 0


def cmd_remap(args) -> int:
    from . import enumerate as enum
    from .reporter import make_state
    config = load_config(args.config)
    state = make_state(config)
    enum.remap(config, state)
    return 0


def cmd_download(args) -> int:
    from . import downloader
    from .reporter import make_state
    config = load_config(args.config)
    state = make_state(config)
    downloader.run(config, state, only_target=args.target)
    return 0


def cmd_clean(args) -> int:
    from . import cleaner
    from .reporter import make_state
    config = load_config(args.config)
    state = make_state(config)
    if args.watch:
        cleaner.watch(config, state, only_target=args.target, interval=args.interval)
    else:
        cleaner.run(config, state, only_target=args.target, force=args.force)
    return 0


def cmd_run(args) -> int:
    from . import cleaner, downloader
    from . import enumerate as enum
    from .reporter import make_state
    config = load_config(args.config)
    state = make_state(config)
    enum.run(config, state, only_target=args.target, max_urls=args.max)
    downloader.run(config, state, only_target=args.target)
    cleaner.run(config, state, only_target=args.target)
    return 0


def cmd_status(args) -> int:
    config = load_config(args.config)
    state = read_state(config.run_dir)
    if state is None:
        print("Status    : idle (no run started yet)")
        return 0

    alive = liveness(state)
    phase = PHASE_LABELS.get(state.get("phase", "idle"), "Idle")
    downloaded = state.get("downloaded", 0)
    total = state.get("total", 0)
    failed = state.get("failed", 0)
    target = state.get("target") or "(all)"

    print(f"Status    : {alive}")
    print(f"Process   : {phase}")
    print(f"Target    : {target}")
    suffix = f"  ({failed} failed)" if failed else ""
    print(f"Downloaded: {downloaded} / {total}{suffix}")
    clean_total = state.get("clean_total", 0)
    if clean_total or state.get("phase") == "cleaning":
        print(f"Cleaned   : {state.get('cleaned', 0)} / {clean_total}")
    rl = state.get("rate_limit") or {}
    if rl.get("waiting"):
        print(f"Rate limit: waiting ({rl.get('reason')}), resume ~{rl.get('resume_at')}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="wayback",
                                     description="Universal Wayback Machine scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_enum = sub.add_parser("enumerate", help="build manifest.json from CDX")
    _add_common(p_enum)
    p_enum.add_argument("--max", type=int, default=None, help="cap number of URLs")
    p_enum.add_argument("--merge", action="store_true",
                        help="add only new URLs to an existing manifest (keep current)")
    p_enum.add_argument("--from", dest="from_ts", default=None,
                        help="override target 'from' window (YYYYMMDD)")
    p_enum.add_argument("--to", dest="to_ts", default=None,
                        help="override target 'to' window (YYYYMMDD)")
    p_enum.set_defaults(func=cmd_enumerate)

    p_rm = sub.add_parser("remap", help="recompute local paths on the existing manifest")
    p_rm.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p_rm.set_defaults(func=cmd_remap)

    p_dl = sub.add_parser("download", help="download manifest URLs")
    _add_common(p_dl)
    p_dl.set_defaults(func=cmd_download)

    p_cl = sub.add_parser("clean", help="clean raw HTML into offline docs")
    _add_common(p_cl)
    p_cl.add_argument("--watch", action="store_true", help="reprocess every N seconds")
    p_cl.add_argument("--interval", type=int, default=60, help="watch interval seconds")
    p_cl.add_argument("--force", action="store_true", help="re-clean existing files")
    p_cl.set_defaults(func=cmd_clean)

    p_run = sub.add_parser("run", help="enumerate -> download -> clean")
    _add_common(p_run)
    p_run.add_argument("--max", type=int, default=None, help="cap number of URLs")
    p_run.set_defaults(func=cmd_run)

    p_st = sub.add_parser("status", help="print live status from state.json")
    p_st.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p_st.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted — progress is saved; re-run to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
