"""Config loading and validation.

Parses config.yaml into typed dataclasses with sane defaults and clear errors,
so the rest of the package never touches raw dicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_MATCH = {"exact", "prefix", "host", "domain"}


class ConfigError(Exception):
    """Raised when config.yaml is missing required fields or malformed."""


@dataclass
class SheetsConfig:
    enabled: bool = False
    service_account_json: str = ""
    spreadsheet_id: str = ""
    worksheet: str = "status"
    update_interval: int = 30


@dataclass
class Target:
    name: str
    url: str
    match: str = "prefix"
    from_ts: str = ""
    to_ts: str = ""
    html_only: bool = True
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    localize_assets: bool = False

    # Compiled regexes (filled in __post_init__)
    include_re: list[re.Pattern] = field(default_factory=list, repr=False)
    exclude_re: list[re.Pattern] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.match not in VALID_MATCH:
            raise ConfigError(
                f"target {self.name!r}: match must be one of {sorted(VALID_MATCH)}, "
                f"got {self.match!r}"
            )
        try:
            self.include_re = [re.compile(p) for p in self.include]
            self.exclude_re = [re.compile(p) for p in self.exclude]
        except re.error as exc:
            raise ConfigError(f"target {self.name!r}: bad regex: {exc}") from exc


@dataclass
class Config:
    run_dir: Path
    threads: int
    request_delay: float
    max_retries: int
    snapshot: str
    ignore_query_params: list[str]
    sheets: SheetsConfig
    targets: list[Target]

    def target(self, name: str) -> Target:
        for t in self.targets:
            if t.name == name:
                return t
        raise ConfigError(f"no target named {name!r} in config")


def _validate_snapshot(value: str) -> str:
    if value in ("first", "last", "latest"):
        return value
    if value.startswith("closest:") and re.fullmatch(r"closest:\d{4,14}", value):
        return value
    raise ConfigError(
        f"snapshot must be first|last|latest|closest:YYYYMMDD, got {value!r}"
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    raw_targets = data.get("targets") or []
    if not raw_targets:
        raise ConfigError("config must define at least one target")

    targets = []
    for i, t in enumerate(raw_targets):
        if "name" not in t or "url" not in t:
            raise ConfigError(f"target #{i} must have 'name' and 'url'")
        targets.append(
            Target(
                name=str(t["name"]),
                url=str(t["url"]),
                match=str(t.get("match", "prefix")),
                from_ts=str(t.get("from", "")),
                to_ts=str(t.get("to", "")),
                html_only=bool(t.get("html_only", True)),
                include=list(t.get("include") or []),
                exclude=list(t.get("exclude") or []),
                localize_assets=bool(t.get("localize_assets", False)),
            )
        )

    sheets_raw = data.get("google_sheets") or {}
    sheets = SheetsConfig(
        enabled=bool(sheets_raw.get("enabled", False)),
        service_account_json=str(sheets_raw.get("service_account_json", "")),
        spreadsheet_id=str(sheets_raw.get("spreadsheet_id", "")),
        worksheet=str(sheets_raw.get("worksheet", "status")),
        update_interval=int(sheets_raw.get("update_interval", 30)),
    )
    if sheets.enabled and not sheets.spreadsheet_id:
        raise ConfigError("google_sheets.enabled is true but spreadsheet_id is empty")

    return Config(
        run_dir=Path(str(data.get("run_dir", "./data"))).expanduser(),
        threads=int(data.get("threads", 4)),
        request_delay=float(data.get("request_delay", 1.0)),
        max_retries=int(data.get("max_retries", 10)),
        snapshot=_validate_snapshot(str(data.get("snapshot", "latest"))),
        ignore_query_params=list(data.get("ignore_query_params") or []),
        sheets=sheets,
        targets=targets,
    )
