"""DMI vKYC - Disposition Analysis scraper (Playwright).

The Selenium original drove Smart Dial's Disposition Report against
onexvoice.k8stech.site. That address no longer serves Smart Dial: it is a
OneXVoice tenant with an entirely different UI, so the original's selectors
(#code, #Submit, #date1, #create-excel) match nothing there and the script
cannot have produced data since the dialer changed.

This drives OneXVoice's Disposition Analysis instead, via the shared flow in
core/onexvoice_disposition.py. Note the shape differs from the Smart Dial
processes: one row per disposition combination with a count, rather than one
row per call. OneXVoice serves no per-call disposition export - Call Analysis
gives daily totals and the Call Log grid has no disposition column.

    Media/DMI_vKYC/Disposition_data/<Y>/<M>/<D>/<date>.csv
    Media/DMI_vKYC/Clean_disposition/<Y>/<M>/<D>/<date>_Disposition.csv

    python script.py 2026-09-14
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from core.onexvoice_disposition import run_disposition

#: A separate tenant on a different dialer product from DMI's, so it keeps its
#: own folder rather than writing into Media/DMI beside the Smart Dial report.
OWNING_PROCESS = "DMI_vKYC"

#: Falls back to the shared ONEXVOICE_* credentials, which are this tenant's.
SETTINGS_PROCESS = "DMI VKYC"

if __name__ == "__main__":
    sys.exit(run_disposition(__file__, settings_process=SETTINGS_PROCESS,
                             process=OWNING_PROCESS))
