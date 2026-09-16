"""Shared Smart Dial Disposition Report scrape, parameterised per process.

Every disposition scraper runs the same flow - log in, Analytics, Disposition
Report, select the three multi-selects, pick the two dates, export - and
differs only in which dialer it points at. That flow lives here so a new
process is a few lines rather than another 400.

    Media/<Process>/Disposition_data/<Y>/<M>/<D>/<date>.xlsx          raw export
    Media/<Process>/Clean_disposition/<Y>/<M>/<D>/<date>_Disposition.csv

Logs go to Media/<Process>/Disposition_logs/, which is a separate tree from
the APR workflow: a disposition run is its own report and has no HRMS stage.
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pandas as pd

import common
from core.browser import BrowserSession, RecoveryRunner
from core.config import settings
from core.errors import CentralErrorHandler, DownloadValidationError
from core.pages.smart_dial import SmartDialDispositionPage
from core.runlog import Stage, start_run

#: A blank Sub Disposition means the agent recorded only a top-level
#: disposition. Downstream needs a value there rather than an empty cell.
SUB_DISPOSITION_PLACEHOLDER = "No Sub Disposition"

#: What every tenant's disposition export carries. Deliberately short: the
#: CRM field set differs per client, and "Date" in particular is absent from
#: DMI's and IRDAI's, which carry Start Time and End Time instead. Requiring
#: it rejected perfectly good exports.
REQUIRED_COLUMNS = ("Campaign", "Agent ID", "Disposition")


def env_key(process, *parts):
    """SMART_DIAL_TN_CM_PASSWORD from a process named "TN CM"."""
    slug = re.sub(r"[^A-Z0-9]+", "_", process.upper()).strip("_")
    return "_".join(("SMART_DIAL", slug) + parts)


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def fill_sub_disposition(frame):
    """Replace blank/whitespace-only Sub Disposition cells with the placeholder."""
    if "Sub Disposition" in frame.columns:
        column = frame["Sub Disposition"].fillna("").astype(str).str.strip()
        frame["Sub Disposition"] = column.mask(column == "",
                                               SUB_DISPOSITION_PLACEHOLDER)
    return frame


def write_headerless_workbook(frame, truncate_at_column, paths, ctx, name):
    """Write the columns up to *truncate_at_column*, with no header row."""
    if truncate_at_column not in frame.columns:
        # Not an error: this export has never carried that column. Saying so
        # beats the original's silent "Error during conversion" print.
        ctx.warn(f"No {truncate_at_column!r} column in the export, so the "
                 f"headerless workbook was not written; the clean CSV has "
                 f"all {len(frame.columns)} columns")
        return None

    index = frame.columns.get_loc(truncate_at_column)
    trimmed = fill_sub_disposition(frame.iloc[:, :index + 1])
    headerless = pd.DataFrame(trimmed.values.tolist())
    directory = paths.dataset(common.FOLDER_CLEAN_DISPOSITION_DATA)
    destination = os.path.join(directory, f"{name}_Clean_Disposition.xlsx")
    headerless.to_excel(destination, header=False, index=False)
    ctx.step(f"Headerless workbook: {os.path.basename(destination)}",
             stage=Stage.PROCESSING)
    return destination


def export_with_style_fallback(page, raw, ctx, tag=""):
    """Export the report, walking Report Styles only if the export is empty.

    The tenant's own default is always tried first: the styles mean different
    things on different dialers (IRDAI's default carries 69 columns and the
    "with CRM field" style only 19), so overriding it would quietly discard
    data. A style that exports nothing is the one case worth retrying - GOQII
    serves a zero-byte body on its default for every date, which is otherwise
    indistinguishable from a day with no calls.
    """
    tried = []
    while True:
        style = page.report_style()
        if style is not None:
            tried.append(style)
        destination, _ = page.download_report(raw)
        try:
            return destination, page.validate_download(destination)
        except DownloadValidationError as empty:
            if not _looks_empty(empty):
                raise
            following = page.next_report_style(tried)
            if following is None:
                raise
            ctx.warn(f"{tag}Report Style {style!r} exported nothing; "
                     f"retrying as {following!r}")


#: Validation failures that mean "the dialer sent us no file", as opposed to a
#: file that parsed cleanly and happens to hold no rows. Only the former is
#: worth another Report Style: an export with a header and no data rows is a
#: real answer about a quiet day, and walking the styles over it would both
#: waste a download per style and bury the empty day under whichever style
#: happens to fail loudest - which is how TN CM leg b's quiet 2026-09-15 came
#: to be reported as "0 bytes" from style 3.
_EMPTY_EXPORT = ("expected at least", "could not be read")


def _looks_empty(error):
    message = str(error).lower()
    return any(marker in message for marker in _EMPTY_EXPORT)


def run_disposition(script_file, *, settings_process=None, leg="",
                    target_date=None, truncate_at_column=None,
                    requires_client_code=True, process=None):
    """Scrape one process's Disposition Report. Returns an exit code.

    `settings_process` names whose SMART_DIAL_* credentials to use when that
    differs from the folder this script lives in - DMI_Disposition is the DMI
    dialer, for instance.

    `truncate_at_column` asks for an extra headerless workbook under
    Clean_disposition_data, cut off after that column. Only Imagine's
    downstream consumes one.

    `requires_client_code` is False for single-tenant dialers, which have no
    client-code field to fill. It stays True everywhere else so a missing
    client code is still caught before the browser starts rather than
    surfacing as a rejected login.
    """
    started = time.time()
    if target_date is None:
        target_date = common.as_date(sys.argv[1]) if len(sys.argv) > 1 else \
            common.as_date(datetime.today() - timedelta(days=1))

    # The disposition report belongs to the process it reports on, not to the
    # folder this script happens to live in: DMI_Disposition/script.py writes
    # Media/DMI/Disposition_data and logs to Media/DMI/Disposition_logs, beside
    # that process's APR data rather than in a folder of its own.
    ctx = start_run(script_file, target_date, process=process,
                    log_folder=common.FOLDER_DISPOSITION_LOGS,
                    workflow_stage="DISPOSITION")
    paths = ctx.paths
    disposition_dir = paths.dataset(common.FOLDER_DISPOSITION)
    clean_csv_dir = paths.dataset(common.FOLDER_CLEAN_DISPOSITION)
    handler = CentralErrorHandler(ctx)
    common.load_env()

    creds = settings_process or ctx.process
    tag = f"[{leg}] " if leg else ""

    def setting(name):
        """Per-leg value, then per-process, then the shared Smart Dial one."""
        keys = ([env_key(creds, leg.upper(), name)] if leg else []) + \
               [env_key(creds, name), f"SMART_DIAL_{name}"]
        for key in keys:
            value = os.getenv(key)
            if value:
                return value
        return ""

    base_url, client_code = setting("URL"), setting("CLIENT_CODE")
    username, password = setting("USERNAME"), setting("PASSWORD")
    required = [("URL", base_url), ("USERNAME", username), ("PASSWORD", password)]
    if requires_client_code:
        required.insert(1, ("CLIENT_CODE", client_code))
    missing = [n for n, v in required if not v]
    if missing:
        ctx.fail(f"Missing dialer settings for {creds}: "
                 + ", ".join(env_key(creds, m) for m in missing)
                 + ". Copy .env.example to .env and fill them in.")
        ctx.failed()
        return 1

    def scrape():
        session = BrowserSession(
            ctx, download_dir=disposition_dir,
            extra_args=[f"--unsafely-treat-insecure-origin-as-secure={_origin(base_url)}"],
        )
        try:
            with ctx.stage_scope(Stage.BROWSER_START):
                session.start()

            runner = RecoveryRunner(ctx, session=session)
            page = SmartDialDispositionPage(session, ctx, runner, base_url,
                                            client_code, username, password)
            # Instance-level, so the shared page object keeps its own contract
            # for anything else that uses it.
            page.REQUIRED_COLUMNS = REQUIRED_COLUMNS
            runner.relogin = page.login

            page.login()
            page.open_analytics()

            # Three multi-selects on this report, not two.
            campaigns = page.select_all_campaigns()
            users = page.select_all_users()
            dispositions = page.select_all_dispositions()
            ctx.step(f"{tag}Campaigns: {campaigns[0]}/{campaigns[1]} | "
                     f"Users: {users[0]}/{users[1]} | "
                     f"Dispositions: {dispositions[0]}/{dispositions[1]}",
                     stage=Stage.SCRAPING)

            date_from, date_to = target_date, target_date + timedelta(days=1)
            page.select_date("from", date_from)
            page.select_date("to", date_to)
            ctx.step(f"{tag}{date_from.isoformat()} -> {date_to.isoformat()}",
                     stage=Stage.DATE_SELECTION)

            raw = os.path.join(disposition_dir, f".download_{ctx.run_id}.xlsx")
            return export_with_style_fallback(page, raw, ctx, tag)
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
        return 1

    name = f"{paths.date_key}_{leg}" if leg else paths.date_key
    # Keep whatever the dialer actually served. Naming a CSV ".xlsx" only
    # moves the problem to whoever opens it next.
    export_path = os.path.join(
        disposition_dir, name + (os.path.splitext(downloaded)[1] or ".xlsx"))
    clean_csv_path = os.path.join(clean_csv_dir, f"{name}_Disposition.csv")

    with ctx.stage_scope(Stage.PROCESSING):
        try:
            if os.path.abspath(downloaded) != os.path.abspath(export_path):
                if os.path.exists(export_path):
                    os.remove(export_path)
                os.replace(downloaded, export_path)
            ctx.step(f"{tag}Export saved: {os.path.basename(export_path)} | "
                     f"{len(raw_frame)} rows | {len(raw_frame.columns)} columns",
                     stage=Stage.PROCESSING)

            # dtype=str throughout: phone numbers and ids must not be coerced
            # into floats and come out as 9.1234e+09.
            frame = SmartDialDispositionPage.read_export(export_path, dtype=str)
            frame = fill_sub_disposition(frame)
            frame.to_csv(clean_csv_path, index=False)
            ctx.step(f"{tag}Clean CSV: {os.path.basename(clean_csv_path)} | "
                     f"{len(frame)} rows", stage=Stage.PROCESSING)

            if truncate_at_column:
                write_headerless_workbook(frame, truncate_at_column, paths,
                                          ctx, name)

            ctx.set_records(scraped=len(frame))
            ctx.step(f"{tag}Final rows: {len(frame)}", stage=Stage.PROCESSING)
        except Exception as exc:
            handler.handle(exc, stage=Stage.PROCESSING)
            ctx.failed()
            return 1

    ctx.success(f"{tag}Completed in {time.time() - started:.2f}s")
    return 0
