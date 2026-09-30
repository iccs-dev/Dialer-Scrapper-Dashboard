#!/usr/bin/env bash
# The nightly job: scrape every process for yesterday, then combine and upload.
#
#     deploy/daily_run.sh              # yesterday, in Asia/Kolkata
#     deploy/daily_run.sh 2026-09-14   # a specific date
#
# Two things this deliberately does not leave to chance:
#
# 1. The report date is computed in Asia/Kolkata and passed explicitly, not
#    left to each script's "yesterday" default. This server's clock is UTC, so
#    between 00:00 and 05:30 IST the UTC date is still the previous day and
#    "yesterday" resolves one day too far back - the scrapers would fetch the
#    wrong day and upload it to HRMS.
#
# 2. Only one run at a time. A scrape that overruns must not have the next
#    night's run start on top of it, both driving browsers at the same dialer.

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="$PROJECT_ROOT/venv/bin/python"
REPORT_TZ="${DIALER_REPORT_TZ:-Asia/Kolkata}"
LOCK="$PROJECT_ROOT/logs/daily_run.lock"
mkdir -p "$PROJECT_ROOT/logs"

if [ $# -ge 1 ]; then
    REPORT_DATE="$1"
else
    REPORT_DATE="$("$PYTHON" - "$REPORT_TZ" <<'PY'
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
print((datetime.now(ZoneInfo(sys.argv[1])).date() - timedelta(days=1)).isoformat())
PY
)"
fi

LOG="$PROJECT_ROOT/logs/daily_run-$REPORT_DATE.log"

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date -Is) another daily run is still going; skipping" >> "$LOG"
    exit 0
fi

{
    echo "=============================================================="
    echo "daily run   report date $REPORT_DATE ($REPORT_TZ)"
    echo "started     $(date -Is)  [machine $(date +%Z)]"
    echo "=============================================================="

    # Each scraper runs in its own subprocess; one failing does not stop the
    # others, and each emails its own alert through the central handler.
    "$PYTHON" run_scrapers.py "$REPORT_DATE"
    scrape_status=$?
    echo "--- run_scrapers.py exited $scrape_status ---"

    # combine.py builds the HRMS upload file and invokes hrms.py. It runs even
    # when a scraper failed: the processes that did work still have data worth
    # uploading, and combine reports what is missing.
    "$PYTHON" combine.py "$REPORT_DATE"
    combine_status=$?
    echo "--- combine.py exited $combine_status ---"

    echo "finished    $(date -Is)"
    exit $(( scrape_status || combine_status ))
} >> "$LOG" 2>&1
