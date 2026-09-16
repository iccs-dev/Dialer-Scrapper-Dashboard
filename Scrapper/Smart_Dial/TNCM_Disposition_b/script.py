"""TNCM_Disposition_b - Disposition Report scraper (Playwright).

Drives the shared flow in core/smart_dial_disposition.py.
TN CM's second dialer login; credentials come from the B leg.

    Media/TN CM/Disposition_data/<Y>/<M>/<D>/<date>.xlsx
    Media/TN CM/Clean_disposition/<Y>/<M>/<D>/<date>_Disposition.csv

    python script.py 2026-09-15
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from core.smart_dial_disposition import run_disposition

#: The process this report belongs to, so its data and logs land beside that
#: process's APR output rather than in a folder named after this script.
OWNING_PROCESS = "TN CM"

#: Whose SMART_DIAL_* credentials this uses - the dialer, not the folder.
SETTINGS_PROCESS = "TN CM"

if __name__ == "__main__":
    sys.exit(run_disposition(__file__, settings_process=SETTINGS_PROCESS,
                             leg="B", process=OWNING_PROCESS))
