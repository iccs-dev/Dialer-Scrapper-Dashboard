"""TN CM_Clean - clean both TN CM legs.

TN CM is scraped under two dialer logins, so there are two exports to clean.
Each leg is cleaned in turn and written to Media/TN CM/APR_Clean/.

    python script.py 2026-09-15
"""

import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

import common

if __name__ == "__main__":
    date_arg = (common.as_date(sys.argv[1]).isoformat() if len(sys.argv) > 1
                else "")
    failures = []
    for leg in ("a.py", "b.py"):
        # sys.executable, not a hardcoded interpreter: the legs must run in
        # whichever virtualenv is running this script.
        command = [sys.executable, os.path.join(SCRIPT_DIR, leg)]
        if date_arg:
            command.append(date_arg)
        if subprocess.run(command, cwd=SCRIPT_DIR).returncode != 0:
            failures.append(leg)
    if failures:
        print(f"TN CM_Clean: {', '.join(failures)} failed")
        sys.exit(1)
