"""
Live Google Sheets integration. When a mod approves a submission, we
increment that Source+Drop's "Drops obtained" cell by 1 -- the sheet's own
formula recalculates "Points gained" automatically, we never touch it.

Each team has their OWN sheet (see team_sheets.py) -- this module opens
whichever sheet_id it's given and caches the connection so repeat approvals
for the same team don't reconnect every time.

Setup required (see README.md for the full walkthrough):
  1. A Google Cloud service account with a JSON key.
  2. That service account's email invited as an Editor on EVERY team's sheet.
  3. GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_WORKSHEET_NAME set in .env.
  4. Each team's sheet ID added to team_sheets.py.

Row numbers are intentionally NOT hardcoded (see region_columns.py) --
we look up the correct row at call time by scanning that region's Source
and Drop columns for a text match. This means inserting/removing/reordering
drop rows in the sheet later is safe and needs no code changes. Only a
change to the overall column layout (a region added/removed, or columns
reordered) would require regenerating region_columns.py.
"""

import os
import logging
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from region_columns import REGION_COLUMNS

log = logging.getLogger("bingo-bot.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_client = None
_worksheets_by_sheet_id = {}


class SheetsNotConfigured(Exception):
    """Raised when the .env Google Sheets settings are missing."""


def is_enabled() -> bool:
    """True if the base Sheets sync setup (service account + worksheet name) is present in .env."""
    return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")) and bool(os.getenv("GOOGLE_WORKSHEET_NAME"))


def _get_client():
    global _client
    if _client is not None:
        return _client

    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not service_account_file:
        raise SheetsNotConfigured(
            "GOOGLE_SERVICE_ACCOUNT_FILE must be set in .env to sync approvals to sheets."
        )

    creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
    _client = gspread.authorize(creds)
    return _client


def _get_worksheet(sheet_id: str):
    if sheet_id in _worksheets_by_sheet_id:
        return _worksheets_by_sheet_id[sheet_id]

    worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME")
    if not worksheet_name:
        raise SheetsNotConfigured(
            "GOOGLE_WORKSHEET_NAME must be set in .env to sync approvals to sheets."
        )

    client = _get_client()
    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name)
    _worksheets_by_sheet_id[sheet_id] = worksheet
    return worksheet


def _find_obtained_cell(ws, region: str, source: str, drop: str) -> Optional[str]:
    """Returns the A1 address of the 'Drops obtained' cell for this
    region/source/drop, or None if no matching row is found."""
    cols = REGION_COLUMNS.get(region)
    if cols is None:
        log.warning(f"No column layout known for region {region!r}")
        return None

    source_col_letter = cols["source_col"]
    drop_col_letter = cols["drop_col"]
    obtained_col_letter = cols["obtained_col"]
    header_row = cols["header_row"]

    source_values = ws.col_values(gspread.utils.a1_to_rowcol(f"{source_col_letter}1")[1])
    drop_values = ws.col_values(gspread.utils.a1_to_rowcol(f"{drop_col_letter}1")[1])

    for i in range(header_row, max(len(source_values), len(drop_values))):
        s = source_values[i].strip() if i < len(source_values) else ""
        d = drop_values[i].strip() if i < len(drop_values) else ""
        if s == source and d == drop:
            row = i + 1  # sheet rows are 1-indexed; list is 0-indexed
            return f"{obtained_col_letter}{row}"

    log.warning(f"No matching row found for {region}/{source}/{drop}")
    return None


def increment_drop_count(sheet_id: str, region: str, source: str, drop: str) -> bool:
    """
    Finds the row in `region`'s block, on the sheet identified by
    `sheet_id`, where Source == source and Drop == drop, and adds 1 to that
    row's "Drops obtained" cell.

    Returns True if a matching row was found and updated, False if no
    matching row exists (sheet may have changed since regions.py was
    generated). Raises SheetsNotConfigured if .env isn't set up, or any
    gspread/network error if the API call itself fails -- callers should
    catch and report both so a sheet hiccup never blocks a Discord approval.
    """
    ws = _get_worksheet(sheet_id)
    obtained_cell = _find_obtained_cell(ws, region, source, drop)
    if obtained_cell is None:
        return False

    current = ws.acell(obtained_cell).value
    try:
        current_count = int(current) if current not in (None, "") else 0
    except ValueError:
        current_count = 0

    ws.update_acell(obtained_cell, current_count + 1)
    log.info(f"Incremented {region}/{source}/{drop} on sheet {sheet_id} -> {obtained_cell} = {current_count + 1}")
    return True


def decrement_drop_count(sheet_id: str, region: str, source: str, drop: str) -> bool:
    """
    The reverse of increment_drop_count -- used when a mod undoes an
    approval. Floors at 0 rather than going negative, in case the sheet
    was already manually edited down since the original approval.
    """
    ws = _get_worksheet(sheet_id)
    obtained_cell = _find_obtained_cell(ws, region, source, drop)
    if obtained_cell is None:
        return False

    current = ws.acell(obtained_cell).value
    try:
        current_count = int(current) if current not in (None, "") else 0
    except ValueError:
        current_count = 0

    new_count = max(0, current_count - 1)
    ws.update_acell(obtained_cell, new_count)
    log.info(f"Decremented {region}/{source}/{drop} on sheet {sheet_id} -> {obtained_cell} = {new_count}")
    return True
