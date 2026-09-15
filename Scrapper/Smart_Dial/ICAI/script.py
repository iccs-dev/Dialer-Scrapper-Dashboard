"""ICAI User Session scraper (Playwright).

Same Smart Dial software as Imagine, on a different host and login, so it
drives the shared SmartDialPage page object rather than repeating the
navigation. Every step runs through RecoveryRunner, and an unrecoverable
failure lands in the central error handler with a screenshot, a trace and an
email alert.

The pandas processing below the scrape is carried over unchanged from the
Selenium version, including the ICAI-only step that drops the first six rows
of the export.
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

# Accept optional date argument (YYYY-MM-DD format)
if len(sys.argv) > 1:
    _target_date = common.as_date(sys.argv[1])
else:
    _target_date = common.as_date(datetime.today() - timedelta(days=1))

# ==================== CONFIGURE PATHS ====================
# The process name comes from the folder this script lives in ("ICAI"), and
# every folder below is built from that process plus the report date.
ctx = start_run(__file__, _target_date)
paths = ctx.paths
apr_data_dir = paths.dataset(common.FOLDER_APR_RAW)
dialer_data_dir = paths.dataset(common.FOLDER_PROCESSED)
csv_data_dir = paths.dataset(common.FOLDER_CSV_COPY)

handler = CentralErrorHandler(ctx)

#: Rows of summary/header the ICAI export carries before the agent rows.
#: Imagine's export does not have them - this is the one processing
#: difference between the two scrapers.
LEADING_ROWS_TO_DROP = 6


def dialer_setting(name, default=""):
    """Per-process override first, then the shared Smart Dial value."""
    process_key = f"SMART_DIAL_{ctx.process.upper()}_{name}"
    return os.getenv(process_key) or os.getenv(f"SMART_DIAL_{name}", default)


BASE_URL = dialer_setting("URL", "http://172.20.122.35/smart/login.php")
CLIENT_CODE = dialer_setting("CLIENT_CODE")
USERNAME = dialer_setting("USERNAME")
PASSWORD = dialer_setting("PASSWORD")

# No hardcoded fallbacks: these live in .env, which is not in the repository.
# A blank credential would reach the dialer as an empty login and come back as
# "invalid user", which is a confusing way to learn that .env is missing.
_missing = [name for name, value in
            (("URL", BASE_URL), ("CLIENT_CODE", CLIENT_CODE),
             ("USERNAME", USERNAME), ("PASSWORD", PASSWORD)) if not value]
if _missing:
    ctx.fail(f"Missing dialer settings for {ctx.process}: "
             + ", ".join(f"SMART_DIAL_{ctx.process.upper()}_{n}" for n in _missing)
             + ". Copy .env.example to .env and fill them in.")
    ctx.failed()
    sys.exit(1)


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# ==================== SCRAPE ====================

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
        # A recovered browser restart or expired session re-authenticates.
        runner.relogin = page_object.login

        page_object.login()
        page_object.open_analytics()

        # Both dropdowns report on one line rather than one line each.
        campaigns = page_object.select_all_campaigns()
        users = page_object.select_all_users()
        ctx.step(f"Campaigns: {campaigns[0]}/{campaigns[1]} | "
                 f"Users: {users[0]}/{users[1]}", stage=Stage.SCRAPING)

        # Preserved from the Selenium version: the report window is
        # [target, target + 1 day].
        date_from = _target_date
        date_to = _target_date + timedelta(days=1)
        page_object.select_date("from", date_from)
        page_object.select_date("to", date_to)
        ctx.step(f"{date_from.isoformat()} -> {date_to.isoformat()}",
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
new_csv_name = paths.report_name("csv")
apr_csv_path = os.path.join(apr_data_dir, new_csv_name)
new_csv_path = os.path.join(dialer_data_dir, new_csv_name)

try:
    downloaded_path, raw_data = scrape()
except Exception:
    ctx.failed()
    sys.exit(1)

# The raw folder keeps the report as .csv. The dialer serves an HTML table
# under an .xls extension, so the download is converted and the original
# removed. `raw_data` is the frame validate_download already parsed.
with ctx.stage_scope(Stage.PROCESSING):
    try:
        raw_data.to_csv(apr_csv_path, index=False)
        os.remove(downloaded_path)
        raw_data.to_csv(new_csv_path, index=False)
        csv_copy_path = os.path.join(csv_data_dir, new_csv_name)
        raw_data.to_csv(csv_copy_path, index=False)
        ctx.detail(f"wrote {ctx.relative(apr_csv_path)}, "
                   f"{ctx.relative(new_csv_path)}, {ctx.relative(csv_copy_path)}")

        # ICAI-only: the export carries six summary rows above the agent rows.
        data = pd.read_csv(new_csv_path)
        data.drop(index=range(0, LEADING_ROWS_TO_DROP)).to_csv(new_csv_path, index=False)
        ctx.step(f"Dropped {LEADING_ROWS_TO_DROP} header rows",
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
                total_index = total_row_index[0]
                data = data.iloc[:total_index]

            columns_to_keep = ["Agent ID", "First Login", "Minutes"]
            data = data[columns_to_keep]

            data.to_csv(new_csv_path, index=False)
            ctx.step("CSV created | Net Login added", stage=Stage.PROCESSING)

            data = pd.read_csv(new_csv_path)
            total_row_index = data[data["Agent ID"].astype(str).str.contains('Total', case=False, na=False)].index
            if len(total_row_index) > 0:
                total_index = total_row_index[0]
                data = data.iloc[:total_index]

            data = data[~data.iloc[:, 0].str.contains('ICAI', case=False, na=False)]

            data['IsWH'] = 'N'
            data.rename(columns={'Agent ID': 'EmpCode', 'First Login': 'Date'}, inplace=True)
            data['EmpCode'] = data['EmpCode'].str.replace(r"\s*\(.*\)", "", regex=True).str.upper()

            # Keep only rows where EmpCode follows the pattern "ATS" + digits
            data = data[data['EmpCode'].str.match(r'^ATS\d+$', na=False)]

            data['Date'] = pd.to_datetime(data['Date'], errors='coerce').dt.strftime('%m-%d-%Y')
            data['Minutes'] = np.ceil(data['Minutes']).astype(int)

            data.to_csv(new_csv_path, index=False)
            ctx.set_records(scraped=len(data))
            ctx.step(f"Final rows: {len(data)}", stage=Stage.PROCESSING)
        else:
            ctx.fail("Required columns ('Login Duration' and "
                     "'Total Break Duration') are missing in the file")
            ctx.failed()
            sys.exit(1)
    except Exception as exc:
        handler.handle(exc, stage=Stage.PROCESSING)
        ctx.failed()
        sys.exit(1)

# One closing line, never a duration line plus a completion line.
ctx.success(f"Completed in {time.time() - start_time:.2f}s")
