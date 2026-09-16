"""One TN CM leg - Smart Dial User Session scraper (Playwright).

TN CM's agents sit under two dialer logins on the same Smart Dial host, so the
report needs two scrapes that script.py then merges. a.py and b.py are the two
entry points; everything they share lives here.

Where TN CM differs from the other Smart Dial processes, and why the shared
page object is not simply reused as-is:

* only the campaign dropdown is selected - the user dropdown was commented out
  in the original and selecting it would change which agents are reported;
* Minutes is Login Duration alone. Imagine, ICAI and DMI subtract Total Break
  Duration; TN CM's export does not carry that column and the original never
  subtracted it;
* the export carries one summary row above the agent rows, not four or six;
* First Login arrives as "14th Sep 2026 09:47:17 AM", so the ordinal suffix is
  stripped before parsing.
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import numpy as np
import pandas as pd

import common
from core.browser import BrowserSession, RecoveryRunner
from core.config import settings
from core.errors import CentralErrorHandler
from core.pages.smart_dial import SmartDialPage
from core.runlog import Stage, start_run

#: Rows of summary the TN CM export carries above the agent rows.
LEADING_ROWS_TO_DROP = 1

#: Total Break Duration is absent from this report, so it cannot be required.
REQUIRED_COLUMNS = ("Agent ID", "Login Duration")

#: "14th Sep 2026 09:47:17 AM" once the ordinal suffix is removed.
LOGIN_TIME_FORMAT = "%d %b %Y %I:%M:%S %p"


def _env_key(process, *parts):
    """SMART_DIAL_TN_CM_PASSWORD from a process named "TN CM".

    Process names come from folder names and may contain spaces, which cannot
    appear in an environment variable.
    """
    slug = re.sub(r"[^A-Z0-9]+", "_", process.upper()).strip("_")
    return "_".join(("SMART_DIAL", slug) + parts)


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def time_to_minutes(value):
    if isinstance(value, str):
        try:
            hours, minutes, seconds = map(int, value.split(":"))
            return hours * 60 + minutes + seconds / 60
        except ValueError:
            return 0
    return 0


def run_leg(leg, script_file, target_date=None):
    """Scrape one TN CM login. Returns a process exit code."""
    started = time.time()
    if target_date is None:
        target_date = common.as_date(sys.argv[1]) if len(sys.argv) > 1 else \
            common.as_date(datetime.today() - timedelta(days=1))

    ctx = start_run(script_file, target_date)
    paths = ctx.paths
    apr_data_dir = paths.dataset(common.FOLDER_APR_RAW)
    dialer_data_dir = paths.dataset(common.FOLDER_PROCESSED)
    csv_data_dir = paths.dataset(common.FOLDER_CSV_COPY)
    handler = CentralErrorHandler(ctx)

    common.load_env()
    process = ctx.process
    base_url = os.getenv(_env_key(process, "URL"), "")
    # Each leg is a different tenant on the same dialer: TN CM's two logins
    # sit under different client codes, so this is per-leg with a shared
    # fallback rather than one value for the process.
    client_code = (os.getenv(_env_key(process, leg.upper(), "CLIENT_CODE"))
                   or os.getenv(_env_key(process, "CLIENT_CODE"), ""))
    username = os.getenv(_env_key(process, leg.upper(), "USERNAME"), "")
    password = (os.getenv(_env_key(process, leg.upper(), "PASSWORD"))
                or os.getenv(_env_key(process, "PASSWORD"), ""))

    missing = [name for name, value in (("URL", base_url),
                                        (f"{leg.upper()}_CLIENT_CODE", client_code),
                                        (f"{leg.upper()}_USERNAME", username),
                                        ("PASSWORD", password)) if not value]
    if missing:
        ctx.fail(f"Missing dialer settings for {process}: "
                 + ", ".join(_env_key(process, m) for m in missing)
                 + ". Copy .env.example to .env and fill them in.")
        ctx.failed()
        return 1

    def scrape():
        session = BrowserSession(
            ctx,
            download_dir=apr_data_dir,
            extra_args=[f"--unsafely-treat-insecure-origin-as-secure={_origin(base_url)}"],
        )
        try:
            with ctx.stage_scope(Stage.BROWSER_START):
                session.start()

            runner = RecoveryRunner(ctx, session=session)
            page_object = SmartDialPage(session, ctx, runner, base_url,
                                        client_code, username, password)
            # Instance-level, so the shared page object keeps its own contract
            # for the processes whose export does carry break duration.
            page_object.REQUIRED_COLUMNS = REQUIRED_COLUMNS
            runner.relogin = page_object.login

            page_object.login()
            page_object.open_analytics()

            # Campaigns only. The original left the user dropdown untouched,
            # and selecting it would change which agents the report covers.
            campaigns = page_object.select_all_campaigns()
            ctx.step(f"[{leg}] Campaigns: {campaigns[0]}/{campaigns[1]}",
                     stage=Stage.SCRAPING)

            date_from = target_date
            date_to = target_date + timedelta(days=1)
            page_object.select_date("from", date_from)
            page_object.select_date("to", date_to)
            ctx.step(f"[{leg}] {date_from.isoformat()} -> {date_to.isoformat()}",
                     stage=Stage.DATE_SELECTION)

            raw = os.path.join(apr_data_dir, f".download_{ctx.run_id}.xls")
            destination, _ = page_object.download_report(raw)
            return destination, page_object.validate_download(destination)
        except Exception as exc:
            handler.handle(exc, page=session.page, session=session,
                           attempts=settings.max_retries + 1)
            raise
        finally:
            session.close()

    csv_name = f"{paths.date_key}_{leg}{common.REPORT_TAG}.csv"
    apr_csv_path = os.path.join(apr_data_dir, csv_name)
    out_path = os.path.join(dialer_data_dir, csv_name)

    try:
        downloaded, raw_data = scrape()
    except Exception:
        ctx.failed()
        return 1

    with ctx.stage_scope(Stage.PROCESSING):
        try:
            raw_data.to_csv(apr_csv_path, index=False)
            os.remove(downloaded)
            raw_data.to_csv(out_path, index=False)
            raw_data.to_csv(os.path.join(csv_data_dir, csv_name), index=False)

            data = pd.read_csv(out_path)
            data.drop(index=range(0, LEADING_ROWS_TO_DROP)).to_csv(out_path, index=False)
            ctx.step(f"[{leg}] Dropped {LEADING_ROWS_TO_DROP} header row",
                     stage=Stage.PROCESSING)
        except Exception as exc:
            handler.handle(exc, stage=Stage.PROCESSING)
            if os.path.exists(out_path):
                os.remove(out_path)
            ctx.failed()
            return 1

    with ctx.stage_scope(Stage.PROCESSING, announce=False):
        try:
            data = pd.read_csv(out_path)
            if "Login Duration" not in data.columns:
                ctx.fail("Required column 'Login Duration' is missing; got "
                         f"{list(data.columns)[:12]}")
                if os.path.exists(out_path):
                    os.remove(out_path)
                ctx.failed()
                return 1

            # No break subtraction: this report has no Total Break Duration.
            data["Minutes"] = data["Login Duration"].apply(time_to_minutes)
            data = data[data["Minutes"] != 0.0]
            data["Agent ID"] = data["Agent ID"].astype(str).str.strip()

            total_rows = data[data["Agent ID"].str.contains("Total", case=False,
                                                            na=False)].index
            if len(total_rows):
                data = data[data.index < total_rows[0]]

            data = data[["Agent ID", "First Login", "Minutes"]]
            data["IsWH"] = "N"
            data.rename(columns={"Agent ID": "EmpCode", "First Login": "Date"},
                        inplace=True)
            data["EmpCode"] = (data["EmpCode"].str.replace(r"\s*\(.*\)", "", regex=True)
                               .str.upper())
            data = data[data["EmpCode"].str.match(r"^ATS\d+$", na=False)]

            # "14th Sep 2026 ..." -> "14 Sep 2026 ..." before parsing.
            data["Date"] = data["Date"].astype(str).str.replace(
                r"(\d+)(st|nd|rd|th)", r"\1", regex=True)
            data["Date"] = pd.to_datetime(data["Date"], format=LOGIN_TIME_FORMAT,
                                          errors="coerce").dt.strftime("%m-%d-%Y")
            data["Minutes"] = np.ceil(data["Minutes"]).astype(int)

            data = data[["EmpCode", "Date", "Minutes", "IsWH"]]
            data.to_csv(out_path, index=False)
            ctx.set_records(scraped=len(data))
            ctx.step(f"[{leg}] Final rows: {len(data)}", stage=Stage.PROCESSING)
        except Exception as exc:
            handler.handle(exc, stage=Stage.PROCESSING)
            if os.path.exists(out_path):
                os.remove(out_path)
            ctx.failed()
            return 1

    ctx.success(f"[{leg}] Completed in {time.time() - started:.2f}s")
    return 0
