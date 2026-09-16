"""Shared Smart Dial APR scrape, parameterised per process.

Every Smart Dial process runs the same flow - log in, Analytics, User Session,
select the dropdowns, pick the two dates, export - and then diverges only in a
handful of values. Copying 200 lines per process is how those copies drift;
this keeps the flow in one place and the differences in the call.

What actually varies, and why each one matters:

* how many summary rows sit above the agent rows in the export
  (Imagine 0, IRDAI 3, TN CM 1, ICAI 6, GOQII 6, Amazon Merchant 9);
* which columns are subtracted from Login Duration to get productive minutes.
  Most subtract "Total Break Duration"; GOQII's export has no such column and
  itemises Lunch, Tea and BioBreak instead; TN CM subtracts nothing at all.
  Getting this wrong silently changes everyone's paid hours;
* whether the user dropdown is selected as well as the campaign one.
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

#: Subtracted from Login Duration by most processes.
TOTAL_BREAK = ("Total Break Duration",)


def env_key(process, *parts):
    """SMART_DIAL_AMAZON_MERCHANT_PASSWORD from a process named "Amazon Merchant".

    Process names come from folder names and may contain spaces, which cannot
    appear in an environment variable.
    """
    slug = re.sub(r"[^A-Z0-9]+", "_", process.upper()).strip("_")
    return "_".join(("SMART_DIAL", slug) + parts)


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def time_to_minutes(value):
    """'08:56:45' -> 536.75. Anything unparseable counts as zero."""
    if isinstance(value, str):
        try:
            hours, minutes, seconds = map(int, value.split(":"))
            return hours * 60 + minutes + seconds / 60
        except ValueError:
            return 0
    return 0


def run_apr_scrape(script_file, *, leading_rows_to_drop=0,
                   break_columns=TOTAL_BREAK, select_users=True,
                   target_date=None, leg=""):
    """Scrape one Smart Dial process's User Session report. Returns an exit code.

    `break_columns` is subtracted from Login Duration; pass () to subtract
    nothing. Every column named there must exist in the export, so it doubles
    as the validation contract.
    """
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
    tag = f"[{leg}] " if leg else ""

    common.load_env()
    process = ctx.process

    def setting(name):
        """Per-leg value, then per-process, then the shared Smart Dial one."""
        keys = ([env_key(process, leg.upper(), name)] if leg else []) + \
               [env_key(process, name), f"SMART_DIAL_{name}"]
        for key in keys:
            value = os.getenv(key)
            if value:
                return value
        return ""

    base_url, client_code = setting("URL"), setting("CLIENT_CODE")
    username, password = setting("USERNAME"), setting("PASSWORD")

    # No hardcoded fallbacks: these live in .env, which is not in the
    # repository. A blank credential reaches the dialer as an empty login and
    # comes back as "invalid user", which is a confusing way to learn that.
    missing = [n for n, v in (("URL", base_url), ("CLIENT_CODE", client_code),
                              ("USERNAME", username), ("PASSWORD", password)) if not v]
    if missing:
        ctx.fail(f"Missing dialer settings for {process}: "
                 + ", ".join(env_key(process, m) for m in missing)
                 + ". Copy .env.example to .env and fill them in.")
        ctx.failed()
        return 1

    required = ("Agent ID", "Login Duration") + tuple(break_columns)

    def scrape():
        session = BrowserSession(
            ctx, download_dir=apr_data_dir,
            extra_args=[f"--unsafely-treat-insecure-origin-as-secure={_origin(base_url)}"],
        )
        try:
            with ctx.stage_scope(Stage.BROWSER_START):
                session.start()

            runner = RecoveryRunner(ctx, session=session)
            page = SmartDialPage(session, ctx, runner, base_url, client_code,
                                 username, password)
            # Instance-level, so the shared page object keeps its own contract
            # for processes whose export carries different columns.
            page.REQUIRED_COLUMNS = required
            runner.relogin = page.login

            page.login()
            page.open_analytics()

            campaigns = page.select_all_campaigns()
            summary = f"{tag}Campaigns: {campaigns[0]}/{campaigns[1]}"
            if select_users:
                users = page.select_all_users()
                summary += f" | Users: {users[0]}/{users[1]}"
            ctx.step(summary, stage=Stage.SCRAPING)

            date_from, date_to = target_date, target_date + timedelta(days=1)
            page.select_date("from", date_from)
            page.select_date("to", date_to)
            ctx.step(f"{tag}{date_from.isoformat()} -> {date_to.isoformat()}",
                     stage=Stage.DATE_SELECTION)

            raw = os.path.join(apr_data_dir, f".download_{ctx.run_id}.xls")
            destination, _ = page.download_report(raw)
            return destination, page.validate_download(destination)
        except Exception as exc:
            handler.handle(exc, page=session.page, session=session,
                           attempts=settings.max_retries + 1)
            raise
        finally:
            session.close()

    csv_name = (f"{paths.date_key}_{leg}{common.REPORT_TAG}.csv" if leg
                else paths.report_name("csv"))
    apr_csv_path = os.path.join(apr_data_dir, csv_name)
    out_path = os.path.join(dialer_data_dir, csv_name)

    try:
        downloaded, raw_data = scrape()
    except Exception:
        ctx.failed()
        return 1

    def discard_partial():
        """Never leave a half-written file for combine.py to pick up."""
        if os.path.exists(out_path):
            os.remove(out_path)
            ctx.detail(f"removed partial {ctx.relative(out_path)}")

    with ctx.stage_scope(Stage.PROCESSING):
        try:
            raw_data.to_csv(apr_csv_path, index=False)
            os.remove(downloaded)
            raw_data.to_csv(out_path, index=False)
            raw_data.to_csv(os.path.join(csv_data_dir, csv_name), index=False)
            if leading_rows_to_drop:
                frame = pd.read_csv(out_path)
                frame.drop(index=range(0, leading_rows_to_drop)).to_csv(out_path, index=False)
                ctx.step(f"{tag}Dropped {leading_rows_to_drop} header row(s)",
                         stage=Stage.PROCESSING)
        except Exception as exc:
            handler.handle(exc, stage=Stage.PROCESSING)
            discard_partial()
            ctx.failed()
            return 1

    with ctx.stage_scope(Stage.PROCESSING, announce=False):
        try:
            data = pd.read_csv(out_path)
            absent = [c for c in required if c not in data.columns]
            if absent:
                ctx.fail(f"Export is missing expected column(s): {', '.join(absent)}; "
                         f"got {list(data.columns)[:12]}")
                discard_partial()
                ctx.failed()
                return 1

            minutes = data["Login Duration"].apply(time_to_minutes)
            for column in break_columns:
                minutes = minutes - data[column].apply(time_to_minutes)
            data["Minutes"] = minutes
            data = data[data["Minutes"] != 0.0]

            data["Agent ID"] = data["Agent ID"].astype(str).str.strip()
            totals = data[data["Agent ID"].str.contains("Total", case=False,
                                                        na=False)].index
            if len(totals):
                # Label, not position: after the filters above the two no
                # longer agree, so .iloc would cut in the wrong place.
                data = data[data.index < totals[0]]

            data = data[["Agent ID", "First Login", "Minutes"]]
            data["IsWH"] = "N"
            data.rename(columns={"Agent ID": "EmpCode", "First Login": "Date"},
                        inplace=True)
            data["EmpCode"] = (data["EmpCode"].str.replace(r"\s*\(.*\)", "", regex=True)
                               .str.upper())
            data = data[data["EmpCode"].str.match(r"^ATS\d+$", na=False)]

            data["Date"] = pd.to_datetime(data["Date"], errors="coerce") \
                             .dt.strftime("%m-%d-%Y")
            data["Minutes"] = np.ceil(data["Minutes"]).astype(int)
            data = data[["EmpCode", "Date", "Minutes", "IsWH"]]
            data.to_csv(out_path, index=False)

            ctx.set_records(scraped=len(data))
            ctx.step(f"{tag}Final rows: {len(data)}", stage=Stage.PROCESSING)
        except Exception as exc:
            handler.handle(exc, stage=Stage.PROCESSING)
            discard_partial()
            ctx.failed()
            return 1

    ctx.success(f"{tag}Completed in {time.time() - started:.2f}s")
    return 0
