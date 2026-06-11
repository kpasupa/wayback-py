"""Optional Google Sheets reporter.

Imported lazily (only when google_sheets.enabled), so gspread/google-auth are not
required for normal use. Pushes the current state as a single row, throttled to
update_interval to respect API quota.
"""

from __future__ import annotations

import time

from .config import SheetsConfig

_HEADER = ["timestamp", "phase", "target", "downloaded", "total", "failed", "pid"]


class SheetsReporter:
    def __init__(self, cfg: SheetsConfig):
        self.cfg = cfg
        self._last = 0.0
        self._ws = None
        self._init_error: str | None = None

    def _worksheet(self):
        if self._ws is not None or self._init_error is not None:
            return self._ws
        try:
            import gspread  # noqa: F401
            gc = gspread.service_account(filename=self.cfg.service_account_json)
            sh = gc.open_by_key(self.cfg.spreadsheet_id)
            try:
                ws = sh.worksheet(self.cfg.worksheet)
            except Exception:
                ws = sh.add_worksheet(self.cfg.worksheet, rows=1000, cols=len(_HEADER))
            if ws.row_values(1) != _HEADER:
                ws.update("A1", [_HEADER])
            self._ws = ws
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)
            print(f"[sheets] disabled (init failed): {exc}")
        return self._ws

    def push(self, state: dict) -> None:
        now = time.time()
        if now - self._last < self.cfg.update_interval:
            return
        ws = self._worksheet()
        if ws is None:
            return
        self._last = now
        row = [
            state.get("heartbeat", ""),
            state.get("phase", ""),
            state.get("target", ""),
            state.get("downloaded", 0),
            state.get("total", 0),
            state.get("failed", 0),
            state.get("pid", 0),
        ]
        try:
            ws.append_row(row, value_input_option="RAW")
        except Exception as exc:  # noqa: BLE001
            print(f"[sheets] append failed: {exc}")
