"""Imagine Disposition Report scraper (Playwright).

Analytics -> Disposition Report, all campaigns, users and dispositions for one
day, exported and written to:

    Media/Imagine/disposition_data/<Y>/<M>/<D>/<date>.xlsx          raw export
    Media/Imagine/Clean_disposition/<Y>/<M>/<D>/<date>_Disposition.csv

Three things the original did are gone, each because they existed only to work
around the old browser stack or the old host:

* clicking "Keep" on chrome://downloads. Chrome holds an .xlsx served over
  plain HTTP as an unsafe download until the user confirms, and that bubble is
  browser UI rather than page DOM, so the original walked shadow roots to
  reach it. Playwright's expect_download() takes the file directly and never
  raises the prompt;
* repairing the workbook through the Windows Excel automation bridge, which
  could not run on this platform at all, so
  it could not run here at all. The export reads cleanly with openpyxl - both
  historic files and a fresh one - so nothing needs repairing;
* the SFTP copy to the share drive, removed as asked.

    python Imagine_Disposition.py 2026-09-14
"""

import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

import common
from core.browser import BrowserSession, RecoveryRunner
from core.config import settings
from core.errors import CentralErrorHandler
from core.pages.smart_dial import SmartDialDispositionPage
from core.runlog import Stage, start_run

import pandas as pd

start_time = time.time()

#: A blank Sub Disposition means the agent recorded only a top-level
#: disposition. Downstream consumers need a value there rather than an empty
#: cell, so say so explicitly. Only blanks are touched.
SUB_DISPOSITION_PLACEHOLDER = "No Sub Disposition"

#: The original truncated the sheet at this column before writing the
#: headerless workbook. The live export has no such column - its header row
#: carries 24 names against 26 data columns - so that step is skipped rather
#: than failing, and says so.
TRUNCATE_AT_COLUMN = "Unique ID"

if len(sys.argv) > 1:
    _target_date = common.as_date(sys.argv[1])
else:
    _target_date = common.as_date(datetime.today() - timedelta(days=1))

ctx = start_run(__file__, _target_date)
paths = ctx.paths
disposition_dir = paths.dataset(common.FOLDER_DISPOSITION)
clean_csv_dir = paths.dataset(common.FOLDER_CLEAN_DISPOSITION)
clean_data_dir = paths.dataset(common.FOLDER_CLEAN_DISPOSITION_DATA)
handler = CentralErrorHandler(ctx)

common.load_env()


def dialer_setting(name):
    """Per-process override first, then the shared Smart Dial value."""
    return (os.getenv(f"SMART_DIAL_{ctx.process.upper()}_{name}")
            or os.getenv(f"SMART_DIAL_{name}", ""))


BASE_URL = dialer_setting("URL")
CLIENT_CODE = dialer_setting("CLIENT_CODE")
USERNAME = dialer_setting("USERNAME")
PASSWORD = dialer_setting("PASSWORD")

_missing = [n for n, v in (("URL", BASE_URL), ("CLIENT_CODE", CLIENT_CODE),
                           ("USERNAME", USERNAME), ("PASSWORD", PASSWORD)) if not v]
if _missing:
    ctx.fail(f"Missing dialer settings for {ctx.process}: "
             + ", ".join(f"SMART_DIAL_{ctx.process.upper()}_{n}" for n in _missing)
             + ". Copy .env.example to .env and fill them in.")
    ctx.failed()
    sys.exit(1)


def fill_sub_disposition(frame):
    """Replace blank/whitespace-only Sub Disposition cells with the placeholder."""
    if "Sub Disposition" in frame.columns:
        column = frame["Sub Disposition"].fillna("").astype(str).str.strip()
        frame["Sub Disposition"] = column.mask(column == "",
                                               SUB_DISPOSITION_PLACEHOLDER)
    return frame


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def scrape():
    """Log in, select everything, pick the dates, export and validate."""
    session = BrowserSession(
        ctx,
        download_dir=disposition_dir,
        extra_args=[f"--unsafely-treat-insecure-origin-as-secure={_origin(BASE_URL)}"],
    )
    try:
        with ctx.stage_scope(Stage.BROWSER_START):
            session.start()

        runner = RecoveryRunner(ctx, session=session)
        page = SmartDialDispositionPage(session, ctx, runner, BASE_URL,
                                        CLIENT_CODE, USERNAME, PASSWORD)
        runner.relogin = page.login

        page.login()
        page.open_analytics()

        # Three multi-selects on this report, not two.
        campaigns = page.select_all_campaigns()
        users = page.select_all_users()
        dispositions = page.select_all_dispositions()
        ctx.step(f"Campaigns: {campaigns[0]}/{campaigns[1]} | "
                 f"Users: {users[0]}/{users[1]} | "
                 f"Dispositions: {dispositions[0]}/{dispositions[1]}",
                 stage=Stage.SCRAPING)

        # Preserved from the original: the window is [target, target + 1 day].
        date_from = _target_date
        date_to = _target_date + timedelta(days=1)
        page.select_date("from", date_from)
        page.select_date("to", date_to)
        ctx.step(f"{date_from.isoformat()} -> {date_to.isoformat()}",
                 stage=Stage.DATE_SELECTION)

        raw = os.path.join(disposition_dir, f".download_{ctx.run_id}.xlsx")
        destination, _ = page.download_report(raw)
        return destination, page.validate_download(destination)
    except Exception as exc:
        handler.handle(exc, page=session.page, session=session,
                       attempts=settings.max_retries + 1)
        raise
    finally:
        session.close()


try:
    downloaded, raw_frame = scrape()
except Exception:
    ctx.failed()
    sys.exit(1)

# ==================== OUTPUTS ====================
export_path = os.path.join(disposition_dir, paths.dated_name("xlsx"))
clean_csv_path = os.path.join(clean_csv_dir, f"{paths.date_key}_Disposition.csv")

with ctx.stage_scope(Stage.PROCESSING):
    try:
        if os.path.abspath(downloaded) != os.path.abspath(export_path):
            if os.path.exists(export_path):
                os.remove(export_path)
            os.replace(downloaded, export_path)
        ctx.step(f"Export saved: {os.path.basename(export_path)} | "
                 f"{len(raw_frame)} rows | {len(raw_frame.columns)} columns",
                 stage=Stage.PROCESSING)

        # dtype=str throughout: phone numbers and ids must not be coerced into
        # floats and come out as 9.1234e+09.
        frame = pd.read_excel(export_path, engine="openpyxl", dtype=str)
        frame = fill_sub_disposition(frame)
        frame.to_csv(clean_csv_path, index=False)
        ctx.step(f"Clean CSV: {os.path.basename(clean_csv_path)} | {len(frame)} rows",
                 stage=Stage.PROCESSING)

        if TRUNCATE_AT_COLUMN in frame.columns:
            index = frame.columns.get_loc(TRUNCATE_AT_COLUMN)
            trimmed = fill_sub_disposition(frame.iloc[:, :index + 1])
            headerless = pd.DataFrame(trimmed.values.tolist())
            clean_xlsx_path = os.path.join(
                clean_data_dir, f"{paths.date_key}_Clean_Disposition.xlsx")
            headerless.to_excel(clean_xlsx_path, header=False, index=False)
            ctx.step(f"Headerless workbook: {os.path.basename(clean_xlsx_path)}",
                     stage=Stage.PROCESSING)
        else:
            # Not an error: this export has never carried that column. Saying
            # so beats the original's silent "Error during conversion" print.
            ctx.warn(f"No {TRUNCATE_AT_COLUMN!r} column in the export, so the "
                     f"headerless workbook was not written; the clean CSV has "
                     f"all {len(frame.columns)} columns")

        ctx.set_records(scraped=len(frame))
        ctx.step(f"Final rows: {len(frame)}", stage=Stage.PROCESSING)
    except Exception as exc:
        handler.handle(exc, stage=Stage.PROCESSING)
        ctx.failed()
        sys.exit(1)

ctx.success(f"Completed in {time.time() - start_time:.2f}s")
