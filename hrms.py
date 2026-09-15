"""Upload the combined productive-hour CSV to HRMS (Playwright).

Flow, matching the Selenium version it replaces:

    login -> Leave Section -> Upload Productive Hour(CSV) -> attach file
    -> Import Data (validation only) -> rejected rows? -> save or skip

*Save Excel* is the commit. When HRMS rejects any agent the save is skipped and
the rejected rows are written to Failed_Agent_list, so combine.py can strip
them and retry with a clean list.

    python hrms.py                    # yesterday
    python hrms.py 2026-09-06         # a specific date
    python hrms.py 2026-09-06 --dry-run   # validate only, never click Save
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
from core.browser import BrowserSession, RecoveryRunner
from core.config import settings
from core.errors import CentralErrorHandler, DownloadValidationError
from core.pages.hrms import HrmsPage
from core.runlog import Stage, start_run

import pandas as pd

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("date", nargs="?", help="report date YYYY-MM-DD (default: yesterday)")
parser.add_argument("--dry-run", action="store_true",
                    help="import and validate but never click Save Excel")
args = parser.parse_args()

if args.date:
    yesterday_date = common.as_date(args.date).strftime(common.DATE_FORMAT)
else:
    yesterday_date = (datetime.now() - timedelta(days=1)).strftime(common.DATE_FORMAT)

def _processes_in_upload(date):
    """Processes named in the upload file, so HRMS lines land in their logs.

    A process missing from the file was not uploaded, and its daily log should
    not claim otherwise.
    """
    upload = common.ProcessPaths(common.UPLOAD_PROCESS, date)
    path = upload.file(common.FOLDER_UPLOAD, upload.dated_name("csv"), create=False)
    try:
        frame = pd.read_csv(path)
    except Exception:
        return []
    if "Process" not in frame.columns:
        return []
    return sorted({str(p) for p in frame["Process"].dropna().unique()})


# This script sits at the project root rather than inside a process folder, so
# its process name is pinned to the one that owns the upload file. HRMS lines
# go into the daily log of each process the upload actually covers.
ctx = start_run(__file__, yesterday_date, process=common.UPLOAD_PROCESS,
                daily_processes=(_processes_in_upload(yesterday_date)
                                 or common.list_data_processes()))
paths = ctx.paths
handler = CentralErrorHandler(ctx)

common.load_env()

# The upload file is looked up in the dated folder first; the older flat
# layout (Media/upload/<date>.csv) is still accepted so an existing feed
# keeps working.
file_path = paths.file(common.FOLDER_UPLOAD, paths.dated_name("csv"))
if not os.path.exists(file_path):
    legacy_path = common.media_path(common.FOLDER_UPLOAD, paths.dated_name("csv"))
    if os.path.exists(legacy_path):
        file_path = legacy_path
        ctx.detail(f"using legacy upload path: {ctx.relative(legacy_path)}")

# Fail fast: without this the missing file is only discovered by the browser
# after a full HRMS login, which buries the cause in a stacktrace.
if not os.path.exists(file_path):
    ctx.fail(f"Upload file not found: {ctx.relative(file_path)}")
    ctx.fail(f"Reason: build it first with `python combine.py {yesterday_date}`")
    ctx.failed()
    sys.exit(1)

failed_agents_folder = paths.dataset(common.FOLDER_FAILED_AGENTS)

HRMS_URL = os.getenv("HRMS_URL", "")
HRMS_USERNAME = os.getenv("HRMS_USERNAME", "")
HRMS_PASSWORD = os.getenv("HRMS_PASSWORD", "")

has_failed_agents = False


def log_upload_breakdown():
    """Log what is being uploaded; returns the data-row count."""
    try:
        upload_df = pd.read_csv(file_path)
        # The file-level line is true for everyone in the upload, so it goes to
        # every contributing process's daily log. The per-process counts are
        # not: each one is routed to its own log, so ICAI's file never reports
        # Imagine's row count.
        ctx.step(f"{os.path.basename(file_path)} | {len(upload_df)} rows",
                 stage=Stage.HRMS_UPLOAD)
        if "Process" in upload_df.columns:
            for proc, count in sorted(upload_df["Process"].value_counts().to_dict().items()):
                ctx.step(f"{proc}: {count} agent(s) in this upload",
                         stage=Stage.HRMS_UPLOAD, only=str(proc))
        return len(upload_df)
    except Exception as exc:
        ctx.warn(f"Could not read upload file for logging: {exc}")
        return None


def upload():
    """Returns True when HRMS saved the records."""
    global has_failed_agents

    session = BrowserSession(ctx, download_dir=failed_agents_folder)
    try:
        with ctx.stage_scope(Stage.BROWSER_START):
            session.start()

        runner = RecoveryRunner(ctx, session=session)
        hrms = HrmsPage(session, ctx, runner, HRMS_URL, HRMS_USERNAME, HRMS_PASSWORD)
        runner.relogin = hrms.login

        hrms.login()
        hrms.open_upload_page()
        hrms.attach(file_path)
        rows = log_upload_breakdown()

        # An empty file gives HRMS nothing to import, so it renders no result
        # grid and no Save Excel button. Without this the run would fail later
        # with "Could not locate Save Excel button", which reads like a UI
        # change and sends people looking in the wrong place.
        if rows == 0:
            with ctx.stage_scope(Stage.HRMS_UPLOAD):
                raise DownloadValidationError(
                    f"Upload file {os.path.basename(file_path)} contains 0 data "
                    "rows (header only); HRMS has nothing to import, so it shows no "
                    "result grid and no Save Excel button"
                )

        rejected = hrms.import_data()
        if rejected:
            has_failed_agents = True
            frame = pd.DataFrame(rejected)
            extracted = os.path.join(failed_agents_folder, paths.dated_name("csv"))
            frame.to_csv(extracted, index=False, header=False)
            ctx.warn(f"{len(frame)} agent(s) rejected; save skipped, "
                     "combine.py will retry with a clean list")
            ctx.detail(f"rejected rows -> {ctx.relative(extracted)}")
            print("HRMS_SKIP_SAVE: Failed agents detected, skipping save.")
            return False

        if args.dry_run:
            ctx.step("Dry run: validated, Save Excel not clicked",
                     stage=Stage.HRMS_UPLOAD)
            print("HRMS_DRY_RUN: validated, nothing saved.")
            return False

        hrms.save()
        message = hrms.confirm_saved()
        if message and "Record Saved Successfully" in message:
            print("Record Saved Successfully!")   # consumed by combine.py
            return True
        ctx.warn("No success message detected after save")
        print("Upload may have failed: No success message detected.")
        return False

    except Exception as exc:
        handler.handle(exc, page=session.page, session=session,
                       attempts=settings.max_retries + 1)
        print(f"HRMS SCRIPT FAILED: {type(exc).__name__}: {exc}")
        raise
    finally:
        session.close()


try:
    saved = upload()
except Exception:
    ctx.failed()
    sys.exit(1)

ok = bool(saved or has_failed_agents or args.dry_run)
if ok:
    ctx.success(f"Completed in {ctx.elapsed_seconds:.2f}s")
else:
    ctx.failed()
sys.exit(0 if ok else 1)
