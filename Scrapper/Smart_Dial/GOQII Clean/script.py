"""GOQII Clean - clean the GOQII APR export.

Reads what the GOQII scraper left in Media/GOQII/APR_data/ and writes the
cleaned, headerless workbook to Media/GOQII/APR_Clean/<Y>/<M>/<D>/.
GOQII's export itemises the breaks rather than carrying a single total.

    python script.py 2026-09-15
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from core.apr_clean import run_clean

#: The process whose export this cleans, and where the output lands.
SOURCE_PROCESS = "GOQII"

#: Subtracted from Login Duration to get productive minutes.
BREAK_COLUMNS = ("Lunch Duration", "Tea Duration", "BioBreak Duration")

if __name__ == "__main__":
    sys.exit(run_clean(__file__, SOURCE_PROCESS,
                       break_columns=BREAK_COLUMNS, leg=""))
