"""DMI leg B - Smart Dial User Session scraper (Playwright).

The same Smart Dial software Imagine uses, on the same host but a different
client code, so this drives the shared SmartDialPage page object rather than
repeating the navigation. The pandas processing below the scrape is carried
over unchanged from the original script, including the DMI-only step that
drops the first four rows of the export.

Output: Media/DMI/dialer_data/<Y>/<M>/<D>/<date>_bAPR.csv - one half of the
report. script.py runs this alongside DMI_a.py and sums the two.

    python DMI_b.py 2026-09-14
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
from core.pages.smart_dial import SmartDialPage
from core.runlog import Stage, start_run

import numpy as np
import pandas as pd

start_time = time.time()

if len(sys.argv) > 1:
    _target_date = common.as_date(sys.argv[1])
else:
    _target_date = common.as_date(datetime.today() - timedelta(days=1))

# The process name comes from the folder this script lives in ("DMI"). The
# script stem is not a workflow stage, so this leg writes no daily-log block
# of its own - script.py owns the DMI workflow entry.
ctx = start_run(__file__, _target_date)
paths = ctx.paths
apr_data_dir = paths.dataset(common.FOLDER_APR_RAW)
dialer_data_dir = paths.dataset(common.FOLDER_PROCESSED)
csv_data_dir = paths.dataset(common.FOLDER_CSV_COPY)

handler = CentralErrorHandler(ctx)

#: Rows of summary/header the DMI export carries before the agent rows.
#: Imagine's export has none and ICAI's has six; this is the only processing
#: difference between the three Smart Dial scrapers.
LEADING_ROWS_TO_DROP = 4

#: This leg's half of the merged report.
LEG_SUFFIX = "b"


def dialer_setting(name, default=""):
    """Per-process override first, then the shared Smart Dial value."""
    process_key = f"SMART_DIAL_{ctx.process.upper()}_{name}"
    return os.getenv(process_key) or os.getenv(f"SMART_DIAL_{name}", default)


common.load_env()
BASE_URL = dialer_setting("URL", "http://172.20.122.100/smart/index.php")
CLIENT_CODE = dialer_setting("CLIENT_CODE", "3050")
USERNAME = dialer_setting("USERNAME", "MIS")
PASSWORD = dialer_setting("PASSWORD", "")


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def scrape():
    """Log in, select everything, pick the dates, export and validate."""
    session = BrowserSession(
        ctx,
        download_dir=apr_data_dir,
        # The flag takes an origin, not a full URL.
        extra_args=[f"--unsafely-treat-insecure-origin-as-secure={_origin(BASE_URL)}"],
    )
    try:
        with ctx.stage_scope(Stage.BROWSER_START):
            session.start()

        runner = RecoveryRunner(ctx, session=session)
        page_object = SmartDialPage(
            session, ctx, runner, BASE_URL, CLIENT_CODE, USERNAME, PASSWORD
        )
        runner.relogin = page_object.login

        page_object.login()
        page_object.open_analytics()

        campaigns = page_object.select_all_campaigns()
        users = page_object.select_all_users()
        ctx.step(f"[{LEG_SUFFIX}] Campaigns: {campaigns[0]}/{campaigns[1]} | "
                 f"Users: {users[0]}/{users[1]}", stage=Stage.SCRAPING)

        # Preserved from the original script: the report window is
        # [target, target + 1 day].
        date_from = _target_date
        date_to = _target_date + timedelta(days=1)
        page_object.select_date("from", date_from)
        page_object.select_date("to", date_to)
        ctx.step(f"[{LEG_SUFFIX}] {date_from.isoformat()} -> {date_to.isoformat()}",
                 stage=Stage.DATE_SELECTION)

        raw_download = os.path.join(apr_data_dir, f".download_{ctx.run_id}.xls")
        destination, _suggested = page_object.download_report(raw_download)
        frame = page_object.validate_download(destination)
        return destination, frame

    except Exception as exc:
        handler.handle(exc, page=session.page, session=session,
                       attempts=settings.max_retries + 1)
        raise
    finally:
        session.close()


# ==================== FILE NAMING ====================
new_csv_name = f"{paths.date_key}_{LEG_SUFFIX}{common.REPORT_TAG}.csv"
apr_csv_path = os.path.join(apr_data_dir, new_csv_name)
new_csv_path = os.path.join(dialer_data_dir, new_csv_name)

try:
    downloaded_path, raw_data = scrape()
except Exception:
    ctx.failed()
    sys.exit(1)

with ctx.stage_scope(Stage.PROCESSING):
    try:
        raw_data.to_csv(apr_csv_path, index=False)
        os.remove(downloaded_path)
        raw_data.to_csv(new_csv_path, index=False)
        csv_copy_path = os.path.join(csv_data_dir, new_csv_name)
        raw_data.to_csv(csv_copy_path, index=False)

        # DMI-only: the export carries four summary rows above the agent rows.
        data = pd.read_csv(new_csv_path)
        data.drop(index=range(0, LEADING_ROWS_TO_DROP)).to_csv(new_csv_path, index=False)
        ctx.step(f"[{LEG_SUFFIX}] Dropped {LEADING_ROWS_TO_DROP} header rows",
                 stage=Stage.PROCESSING)
    except Exception as exc:
        handler.handle(exc, stage=Stage.PROCESSING)
        ctx.failed()
        sys.exit(1)


# ==================== PROCESS CSV DATA ====================
def time_to_minutes(time_str):
    if isinstance(time_str, str):
        try:
            h, m, s = map(int, time_str.split(":"))
            return h * 60 + m + s / 60
        except ValueError:
            return 0
    return 0


with ctx.stage_scope(Stage.PROCESSING, announce=False):
    try:
        data = pd.read_csv(new_csv_path)

        if "Login Duration" in data.columns and "Total Break Duration" in data.columns:
            data["Login Duration (minutes)"] = data["Login Duration"].apply(time_to_minutes)
            data["Total Break Duration (minutes)"] = data["Total Break Duration"].apply(time_to_minutes)

            data["Minutes"] = (data["Login Duration (minutes)"] - data["Total Break Duration (minutes)"])
            data = data[data["Minutes"] != 0.0]

            data["Agent ID"] = data["Agent ID"].str.strip()

            total_row_index = data[data["##"].astype(str).str.contains('Total', case=False, na=False)].index
            if len(total_row_index) > 0:
                # .iloc would treat this label as a position; after the filters
                # above the two no longer agree, so the cut lands in the wrong
                # place. Select by label instead.
                data = data[data.index < total_row_index[0]]

            data = data[["Agent ID", "First Login", "Minutes"]]
            data.to_csv(new_csv_path, index=False)
            ctx.step(f"[{LEG_SUFFIX}] CSV created | Net Login added", stage=Stage.PROCESSING)

            data = pd.read_csv(new_csv_path)
            total_row_index = data[data["Agent ID"].astype(str).str.contains('Total', case=False, na=False)].index
            if len(total_row_index) > 0:
                # .iloc would treat this label as a position; after the filters
                # above the two no longer agree, so the cut lands in the wrong
                # place. Select by label instead.
                data = data[data.index < total_row_index[0]]

            data['IsWH'] = 'N'
            data.rename(columns={'Agent ID': 'EmpCode', 'First Login': 'Date'}, inplace=True)
            data['EmpCode'] = data['EmpCode'].str.replace(r"\s*\(.*\)", "", regex=True).str.upper()

            # Keep only rows where EmpCode follows the pattern "ATS" + digits
            data = data[data['EmpCode'].str.match(r'^ATS\d+$', na=False)]

            data['Date'] = pd.to_datetime(data['Date'], errors='coerce').dt.strftime('%m-%d-%Y')
            data['Minutes'] = np.ceil(data['Minutes']).astype(int)

            data = data[["EmpCode", "Date", "Minutes", "IsWH"]]
            data.to_csv(new_csv_path, index=False)
            ctx.set_records(scraped=len(data))
            ctx.step(f"[{LEG_SUFFIX}] Final rows: {len(data)}", stage=Stage.PROCESSING)
        else:
            ctx.fail("Required columns ('Login Duration' and "
                     "'Total Break Duration') are missing in the file")
            ctx.failed()
            sys.exit(1)
    except Exception as exc:
        handler.handle(exc, stage=Stage.PROCESSING)
        # Remove the half-written file so the orchestrator cannot merge it.
        for stale in (new_csv_path,):
            if os.path.exists(stale):
                os.remove(stale)
                ctx.detail(f"removed partial {ctx.relative(stale)}")
        ctx.failed()
        sys.exit(1)

ctx.success(f"[{LEG_SUFFIX}] Completed in {time.time() - start_time:.2f}s")
