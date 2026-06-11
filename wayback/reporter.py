"""Single place that publishes run status.

Always persists state.json (via State's own atomic write); optionally also pushes
to Google Sheets when configured. Phases create one State wired to this reporter.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .state import State


def make_state(config: Config) -> State:
    """Build a State whose flushes also fan out to enabled reporters."""
    on_flush = None
    if config.sheets.enabled:
        from .sheets import SheetsReporter  # lazy import; optional dependency
        reporter = SheetsReporter(config.sheets)
        on_flush = reporter.push
    return State(Path(config.run_dir), on_flush=on_flush)
