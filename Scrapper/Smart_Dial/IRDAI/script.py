"""IRDAI User Session scraper (Playwright).

Drives the shared Smart Dial flow in core/smart_dial_apr.py; only this
process's differences live here. The pandas processing is carried over
unchanged from the original script.

Three summary rows above the data.

    python script.py 2026-09-14
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from core.smart_dial_apr import run_apr_scrape

#: Summary rows this export carries above the agent rows.
LEADING_ROWS_TO_DROP = 3

#: Subtracted from Login Duration to get productive minutes.
BREAK_COLUMNS = ("Total Break Duration",)

if __name__ == "__main__":
    sys.exit(run_apr_scrape(
        __file__,
        leading_rows_to_drop=LEADING_ROWS_TO_DROP,
        break_columns=BREAK_COLUMNS,
    ))
