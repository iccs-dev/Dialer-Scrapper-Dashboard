"""TN CM APR scraper - runs both legs and merges them.

TN CM's agents sit under two dialer logins - two client codes on one Smart
Dial host - so the report needs two scrapes:

    a.py   ->  <date>_aAPR.csv
    b.py   ->  <date>_bAPR.csv

Each leg's host, client code, user and password come from .env as
SMART_DIAL_TN_CM_[A|B]_*; nothing is hardcoded here.

The legs run one after the other, as in the original, and their minutes are
summed per agent to produce the file combine.py picks up:

    Media/TN CM/dialer_data/<Y>/<M>/<D>/<date>_APR.csv

This script owns the TN CM entry in the daily log and is what reports to the
dashboard; the legs are steps inside it.

    python script.py 2026-09-14
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

import common
from core.errors import CentralErrorHandler
from core.runlog import Stage, start_run

import pandas as pd

start_time = time.time()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

#: suffix -> the script that produces that half.
LEGS = (("a", "a.py"), ("b", "b.py"))

#: A leg drives a browser through a full report; give it room.
LEG_TIMEOUT_SECONDS = int(os.getenv("TN_CM_LEG_TIMEOUT_SECONDS", "1800"))

if len(sys.argv) > 1:
    _target_date = common.as_date(sys.argv[1])
else:
    _target_date = common.as_date(datetime.today() - timedelta(days=1))

ctx = start_run(__file__, _target_date)
paths = ctx.paths
dialer_data_dir = paths.dataset(common.FOLDER_PROCESSED)
handler = CentralErrorHandler(ctx)


def leg_path(suffix):
    """Where a leg leaves its half of the report."""
    return os.path.join(dialer_data_dir,
                        f"{paths.date_key}_{suffix}{common.REPORT_TAG}.csv")


def _relay(suffix, output, returncode):
    """Re-log a leg's output at the level it was written.

    A leg writes no daily-log block of its own, so anything it flags is only
    visible if this script repeats it.
    """
    for line in (output or "").splitlines():
        text = line.rstrip()
        if not text.strip():
            continue
        if "[WARN]" in text:
            ctx.warn(f"leg {suffix}: {text.split('[WARN]', 1)[1].strip()}")
        elif "[ERROR]" in text and returncode not in (0, None):
            ctx.fail(f"leg {suffix}: {text.split('[ERROR]', 1)[1].strip()}")
        else:
            ctx.detail(f"  {suffix}: {text}")


def run_legs():
    """Run each leg in turn. Returns {suffix: returncode}."""
    # sys.executable, not a hardcoded interpreter: the legs must run in
    # whichever virtualenv is running this script.
    child_env = {
        **os.environ,
        # The legs report nothing to the dashboard - this script does, once,
        # for the whole of TN CM. Without this each leg would open its own run.
        "MONITOR_API_URL": "",
        "DIALER_RUN_ID": "",
    }
    results = {}
    for suffix, filename in LEGS:
        ctx.detail(f"leg {suffix}: starting {filename}")
        try:
            completed = subprocess.run(
                [sys.executable, os.path.join(SCRIPT_DIR, filename), paths.date_key],
                cwd=SCRIPT_DIR, env=child_env, timeout=LEG_TIMEOUT_SECONDS,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            results[suffix] = completed.returncode
            output = completed.stdout
        except subprocess.TimeoutExpired as expired:
            results[suffix] = None
            output = expired.stdout or ""
            ctx.fail(f"leg {suffix}: no output within {LEG_TIMEOUT_SECONDS}s; killed")
        _relay(suffix, output, results[suffix])
    return results


with ctx.stage_scope(Stage.SCRAPING):
    outcomes = run_legs()
    for suffix, code in sorted(outcomes.items()):
        produced = os.path.exists(leg_path(suffix))
        if code == 0 and produced:
            ctx.step(f"leg {suffix}: ok", stage=Stage.SCRAPING)
        elif produced:
            ctx.warn(f"leg {suffix}: exit {code}; its partial file is ignored")
        else:
            ctx.fail(f"leg {suffix}: exit {code} and no output file")

# A leg that failed part-way can still have written its raw export before
# falling over, so a leg contributes only when it exited cleanly and left a file.
available = [s for s, _ in LEGS
             if outcomes.get(s) == 0 and os.path.exists(leg_path(s))]
missing = [s for s, _ in LEGS if s not in available]

if not available:
    ctx.fail("Neither leg produced a file; nothing to merge")
    ctx.failed()
    sys.exit(1)

# The original said so explicitly: a partial upload beats none, because the
# half that worked is still real attendance. It is a warning, not a silent
# success - the other half's agents will be missing from HRMS.
if missing:
    ctx.warn(f"PARTIAL DATA: only leg(s) {', '.join(available)} available; "
             f"agent minutes from {', '.join(missing)} are missing")

output_file = os.path.join(dialer_data_dir, paths.report_name("csv"))

with ctx.stage_scope(Stage.PROCESSING):
    try:
        frames = []
        for suffix in available:
            frame = pd.read_csv(leg_path(suffix))
            if frame.empty:
                ctx.warn(f"leg {suffix}: file is empty, not included")
                continue
            ctx.step(f"leg {suffix}: {len(frame)} agent(s)", stage=Stage.PROCESSING)
            frames.append(frame)

        if not frames:
            ctx.fail("Both leg files are empty; nothing to upload")
            ctx.failed()
            sys.exit(1)

        combined = pd.concat(frames, ignore_index=True)

        # An agent can appear under both logins in one day, so their minutes
        # are summed rather than one row winning.
        result = (combined
                  .groupby(["EmpCode", "Date", "IsWH"], as_index=False)["Minutes"]
                  .sum())
        result = result[["EmpCode", "Date", "Minutes", "IsWH"]]
        result.to_csv(output_file, index=False)

        ctx.set_records(scraped=len(result))
        ctx.step(f"Final rows: {len(result)} (merged from {len(combined)} leg rows)",
                 stage=Stage.PROCESSING)
        ctx.detail(f"wrote {ctx.relative(output_file)}")

        # The halves have served their purpose; the merged file is the report.
        for suffix in available:
            if os.path.exists(leg_path(suffix)):
                os.remove(leg_path(suffix))
                ctx.detail(f"removed {ctx.relative(leg_path(suffix))}")
    except Exception as exc:
        handler.handle(exc, stage=Stage.PROCESSING)
        ctx.failed()
        sys.exit(1)

if ctx.error_count:
    # ctx.success() downgrades itself to FAILED when errors were logged; the
    # exit code has to agree or run_scrapers.py and the dashboard see a pass.
    ctx.failed()
    sys.exit(1)
ctx.success(f"Completed in {time.time() - start_time:.2f}s")
