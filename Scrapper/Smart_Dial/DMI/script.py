"""DMI APR scraper - runs both legs and merges them.

DMI's agents work across two dialers, so one report needs two scrapes:

    DMI_a.py  OneXVoice  ->  <date>_aAPR.csv
    DMI_b.py  Smart Dial ->  <date>_bAPR.csv

Both legs run at the same time, then their minutes are summed per agent to
produce the file combine.py picks up:

    Media/DMI/dialer_data/<Y>/<M>/<D>/<date>_APR.csv

This script owns the DMI workflow entry in the daily log and is the one that
reports to the dashboard; the legs are steps inside it.

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
LEGS = (("a", "DMI_a.py"), ("b", "DMI_b.py"))

#: A leg drives a browser through a full report; give it room.
LEG_TIMEOUT_SECONDS = int(os.getenv("DMI_LEG_TIMEOUT_SECONDS", "1800"))

if len(sys.argv) > 1:
    _target_date = common.as_date(sys.argv[1])
else:
    _target_date = common.as_date(datetime.today() - timedelta(days=1))

ctx = start_run(__file__, _target_date)
paths = ctx.paths
dialer_data_dir = paths.dataset(common.FOLDER_PROCESSED)
handler = CentralErrorHandler(ctx)


def _flag(name, default=False):
    """Read a yes/no setting from the environment."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def leg_path(suffix):
    """Where a leg leaves its half of the report."""
    return os.path.join(dialer_data_dir,
                        f"{paths.date_key}_{suffix}{common.REPORT_TAG}.csv")


def run_legs():
    """Start both legs together and wait. Returns {suffix: returncode}."""
    # sys.executable, not a hardcoded interpreter path: the legs must run in
    # whichever virtualenv is running this script.
    child_env = {
        **os.environ,
        # The legs report nothing to the dashboard - this script does, once,
        # for the whole of DMI. Without this each leg would open its own run.
        "MONITOR_API_URL": "",
        "DIALER_RUN_ID": "",
    }
    started = {}
    for suffix, filename in LEGS:
        started[suffix] = subprocess.Popen(
            [sys.executable, os.path.join(SCRIPT_DIR, filename),
             paths.date_key],
            cwd=SCRIPT_DIR, env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        ctx.detail(f"leg {suffix}: started {filename}")

    results = {}
    for suffix, process in started.items():
        try:
            output, _ = process.communicate(timeout=LEG_TIMEOUT_SECONDS)
            results[suffix] = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            results[suffix] = None
            ctx.fail(f"leg {suffix}: no output within {LEG_TIMEOUT_SECONDS}s; killed")
        _relay(suffix, output, results[suffix])
    return results


def _relay(suffix, output, returncode):
    """Re-log a leg's output at the level it was written.

    A leg writes no daily-log block of its own, so anything it flags is only
    visible if this script repeats it. Relaying everything as a debug detail
    hid real warnings - an agent id with no ATS code, for instance, which HRMS
    rejects the next morning.
    """
    for line in (output or "").splitlines():
        text = line.rstrip()
        if not text.strip():
            continue
        if "[WARN]" in text:
            ctx.warn(f"leg {suffix}: {text.split('[WARN]', 1)[1].strip()}")
        elif "[ERROR]" in text and returncode not in (0, None):
            # The leg's exit code already fails the run; these lines say why.
            ctx.fail(f"leg {suffix}: {text.split('[ERROR]', 1)[1].strip()}")
        else:
            ctx.detail(f"  {suffix}: {text}")


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
# falling over. Merging that would mix unprocessed rows into the report, so a
# leg contributes only when it both exited cleanly and left a file.
available = [s for s, _ in LEGS
             if outcomes.get(s) == 0 and os.path.exists(leg_path(s))]
missing = [s for s, _ in LEGS if s not in available]

# DMI's agents are split across the two dialers, so one leg is half the
# workforce. Writing that as if it were the day's report would send combine.py
# a short file and HRMS would record everyone on the other dialer as absent -
# a silent data error rather than a visible failure. The original script also
# stopped when a leg failed. Set DMI_ALLOW_PARTIAL=true to override when a
# dialer is knowingly down and half is better than nothing.
if missing and not _flag("DMI_ALLOW_PARTIAL"):
    ctx.fail(f"leg(s) {', '.join(missing)} did not complete; refusing to write a "
             f"partial report. Re-run, or set DMI_ALLOW_PARTIAL=true to merge "
             f"only leg(s) {', '.join(available) or 'none'}")
    ctx.failed()
    sys.exit(1)
if not available:
    ctx.fail("Neither leg produced a file; nothing to merge")
    ctx.failed()
    sys.exit(1)
if missing:
    ctx.warn(f"DMI_ALLOW_PARTIAL is set: merging only leg(s) "
             f"{', '.join(available)}; {', '.join(missing)} missing")

# ==================== MERGE ====================
output_file = os.path.join(dialer_data_dir, paths.report_name("csv"))

with ctx.stage_scope(Stage.PROCESSING):
    try:
        frames = []
        for suffix in available:
            frame = pd.read_csv(leg_path(suffix))
            ctx.step(f"leg {suffix}: {len(frame)} agent(s)", stage=Stage.PROCESSING)
            frames.append(frame)

        combined = pd.concat(frames, ignore_index=True)

        # An agent can log in on both dialers in one day, so their minutes are
        # summed rather than one row winning.
        result = (combined
                  .groupby(['EmpCode', 'Date', 'IsWH'], as_index=False)['Minutes']
                  .sum())
        result = result[['EmpCode', 'Date', 'Minutes', 'IsWH']]
        result.to_csv(output_file, index=False)

        # HRMS keys on the ATS employee code. Anything else in this column is
        # an agent OneXVoice knows but ATSIDs.xlsx does not, and HRMS will
        # reject that row tomorrow - so say so now, while the file is being
        # written, rather than leaving it to the upload to discover.
        unmapped = result[~result["EmpCode"].astype(str)
                          .str.match(r"^ATS\d+$", na=False)]
        if len(unmapped):
            sample = ", ".join(unmapped["EmpCode"].astype(str).head(10))
            ctx.warn(f"{len(unmapped)} of {len(result)} agent id(s) have no ATS "
                     f"code and will be rejected by HRMS: {sample}"
                     + (" ..." if len(unmapped) > 10 else "")
                     + ". Add them to ATSIDs.xlsx.")

        ctx.set_records(scraped=len(result))
        ctx.step(f"Final rows: {len(result)} (merged from "
                 f"{len(combined)} leg rows)", stage=Stage.PROCESSING)
        ctx.detail(f"wrote {ctx.relative(output_file)}")

        # The halves have served their purpose; the merged file is the report.
        for suffix in available:
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
