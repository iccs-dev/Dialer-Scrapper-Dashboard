"""Shared OneXVoice Disposition Analysis scrape, parameterised per process.

The counterpart to core/smart_dial_disposition.py for tenants on OneXVoice
rather than Smart Dial. The outputs land in the same places so the dashboard
and the Disposition Status tab treat every process alike:

    Media/<Process>/Disposition_data/<Y>/<M>/<D>/<date>.<served extension>
    Media/<Process>/Clean_disposition/<Y>/<M>/<D>/<date>_Disposition.csv

Logs go to Media/<Process>/Disposition_logs/.

The report itself is not shaped like Smart Dial's: it is one row per
disposition combination with a count, not one row per call. That is what this
dialer offers - see OneXVoiceDispositionPage for what was ruled out and why.
"""

import os
import sys
import time
from datetime import datetime, timedelta

import common
from core.browser import BrowserSession, RecoveryRunner
from core.config import settings
from core.errors import CentralErrorHandler
from core.pages.onexvoice import OneXVoiceDispositionPage
from core.runlog import Stage, start_run


def env_key(process, *parts):
    """ONEXVOICE_DMI_VKYC_PASSWORD from a process named "DMI VKYC"."""
    import re

    slug = re.sub(r"[^A-Z0-9]+", "_", process.upper()).strip("_")
    return "_".join(("ONEXVOICE", slug) + parts)


def run_disposition(script_file, *, settings_process=None, target_date=None,
                    process=None):
    """Scrape one process's Disposition Analysis. Returns an exit code."""
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

    def setting(name):
        """Per-process value, then the shared OneXVoice one."""
        return os.getenv(env_key(creds, name)) or os.getenv(f"ONEXVOICE_{name}") or ""

    base_url = setting("URL")
    username, password = setting("USERNAME"), setting("PASSWORD")
    missing = [n for n, v in (("URL", base_url), ("USERNAME", username),
                              ("PASSWORD", password)) if not v]
    if missing:
        ctx.fail(f"Missing dialer settings for {creds}: "
                 + ", ".join(env_key(creds, m) for m in missing)
                 + ". Copy .env.example to .env and fill them in.")
        ctx.failed()
        return 1

    def scrape():
        session = BrowserSession(ctx, download_dir=disposition_dir)
        try:
            with ctx.stage_scope(Stage.BROWSER_START):
                session.start()

            runner = RecoveryRunner(ctx, session=session)
            page = OneXVoiceDispositionPage(session, ctx, runner, base_url,
                                            username, password)
            runner.relogin = page.login

            page.login()
            page.open_report()
            page.select_date(target_date)

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
        return 1

    name = paths.date_key
    # Keep whatever the dialer actually served rather than renaming a CSV
    # to .xlsx and moving the problem to whoever opens it next.
    export_path = os.path.join(
        disposition_dir, name + (os.path.splitext(downloaded)[1] or ".xlsx"))
    clean_csv_path = os.path.join(clean_csv_dir, f"{name}_Disposition.csv")

    with ctx.stage_scope(Stage.PROCESSING):
        try:
            if os.path.abspath(downloaded) != os.path.abspath(export_path):
                if os.path.exists(export_path):
                    os.remove(export_path)
                os.replace(downloaded, export_path)
            ctx.step(f"Export saved: {os.path.basename(export_path)} | "
                     f"{len(raw_frame)} rows | {len(raw_frame.columns)} columns",
                     stage=Stage.PROCESSING)

            frame = OneXVoiceDispositionPage.read_export(export_path, dtype=str)
            # The dialer pads every level with surrounding spaces, so the same
            # disposition reads as two different strings downstream.
            for column in frame.columns:
                frame[column] = frame[column].fillna("").astype(str).str.strip()
            frame.to_csv(clean_csv_path, index=False)
            ctx.step(f"Clean CSV: {os.path.basename(clean_csv_path)} | "
                     f"{len(frame)} rows", stage=Stage.PROCESSING)

            ctx.set_records(scraped=len(frame))
            ctx.step(f"Final rows: {len(frame)}", stage=Stage.PROCESSING)
        except Exception as exc:
            handler.handle(exc, stage=Stage.PROCESSING)
            ctx.failed()
            return 1

    ctx.success(f"Completed in {time.time() - started:.2f}s")
    return 0
