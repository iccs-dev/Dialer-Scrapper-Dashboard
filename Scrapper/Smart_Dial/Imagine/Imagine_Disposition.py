"""Imagine - Disposition Report scraper (Playwright).

Drives the shared flow in core/smart_dial_disposition.py, which every
Disposition scraper now uses. Imagine is the one process whose downstream also
wants a headerless workbook cut off after "Unique ID".

    Media/Imagine/Disposition_data/<Y>/<M>/<D>/<date>.xlsx
    Media/Imagine/Clean_disposition/<Y>/<M>/<D>/<date>_Disposition.csv
    Media/Imagine/Clean_disposition_data/<Y>/<M>/<D>/<date>_Clean_Disposition.xlsx

    python Imagine_Disposition.py 2026-09-14

Logs go to Media/Imagine/Disposition_logs/, kept separate from the APR logs.
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from core.smart_dial_disposition import run_disposition

#: The export is trimmed here for the headerless workbook. Absent from the
#: current export, which the run reports rather than failing over.
TRUNCATE_AT_COLUMN = "Unique ID"

if __name__ == "__main__":
    sys.exit(run_disposition(__file__,
                             truncate_at_column=TRUNCATE_AT_COLUMN))
