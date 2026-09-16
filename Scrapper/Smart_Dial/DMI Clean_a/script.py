"""DMI Clean_a - clean the DMI APR export.

Reads what the DMI scraper left in Media/DMI/APR_data/ and writes the
cleaned, headerless workbook to Media/DMI/APR_Clean/<Y>/<M>/<D>/.
Leg a is the OneXVoice half, whose export names the column "Total Breaks".

    python script.py 2026-09-15
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from core.apr_clean import run_clean

#: The process whose export this cleans, and where the output lands.
SOURCE_PROCESS = "DMI"

#: Subtracted from Login Duration to get productive minutes.
BREAK_COLUMNS = ("Total Breaks",)

if __name__ == "__main__":
    sys.exit(run_clean(__file__, SOURCE_PROCESS,
                       break_columns=BREAK_COLUMNS, leg="a",
                       # OneXVoice names these differently from Smart Dial.
                       agent_column="Agent Id", login_column="Login",
                       # Same recovery the DMI_a scraper does: this export
                       # leaves Login Duration at 00:00:00 for real sessions.
                       logout_column="Logout",
                       # OneXVoice numbers its agents; downstream wants ATS codes.
                       id_mapping_env="DMI_ATS_MAPPING"))
