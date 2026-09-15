"""DMI leg A - OneXVoice Agent Performance scraper (Playwright).

OneXVoice is a different platform from Smart Dial, so this drives the
OneXVoicePage page object. The pandas processing below the scrape is carried
over unchanged from the original script, including the agent-id to ATS-code
mapping, which is what makes the output acceptable to HRMS.

Output: Media/DMI/dialer_data/<Y>/<M>/<D>/<date>_aAPR.csv - one half of the
report. script.py runs this alongside DMI_b.py and sums the two.

    python DMI_a.py 2026-09-14
"""

import os
import sys
import time
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

import common
from core.browser import BrowserSession, RecoveryRunner
from core.config import settings
from core.errors import CentralErrorHandler
from core.pages.onexvoice import OneXVoicePage
from core.runlog import Stage, start_run

import numpy as np
import pandas as pd

start_time = time.time()

if len(sys.argv) > 1:
    _target_date = common.as_date(sys.argv[1])
else:
    _target_date = common.as_date(datetime.today() - timedelta(days=1))

ctx = start_run(__file__, _target_date)
paths = ctx.paths
apr_data_dir = paths.dataset(common.FOLDER_APR_RAW)
dialer_data_dir = paths.dataset(common.FOLDER_PROCESSED)
csv_data_dir = paths.dataset(common.FOLDER_CSV_COPY)

handler = CentralErrorHandler(ctx)

#: This leg's half of the merged report.
LEG_SUFFIX = "a"

common.load_env()
BASE_URL = os.getenv("ONEXVOICE_URL",
                     "https://onexvoice.k8stech.site/telephony/0/dashboard/")
USERNAME = os.getenv("ONEXVOICE_USERNAME", "")
PASSWORD = os.getenv("ONEXVOICE_PASSWORD", "")

#: OneXVoice identifies agents by its own numeric id. HRMS only accepts ATS
#: employee codes, so the export is useless without this mapping.
MAPPING_FILE = os.path.join(
    common.PROJECT_ROOT,
    os.getenv("DMI_ATS_MAPPING", os.path.join("Media", "DMI", "ATSIDs.xlsx")),
)


def scrape():
    """Log in, pick the day, export and validate."""
    session = BrowserSession(ctx, download_dir=apr_data_dir)
    try:
        with ctx.stage_scope(Stage.BROWSER_START):
            session.start()

        runner = RecoveryRunner(ctx, session=session)
        page_object = OneXVoicePage(session, ctx, runner, BASE_URL, USERNAME, PASSWORD)
        runner.relogin = page_object.login

        page_object.login()
        page_object.open_report()
        page_object.select_date(_target_date)

        raw_download = os.path.join(apr_data_dir, f".download_{ctx.run_id}.csv")
        destination, _suggested = page_object.download_report(raw_download)
        frame = page_object.validate_download(destination)
        return destination, frame

    except Exception as exc:
        handler.handle(exc, page=session.page, session=session,
                       attempts=settings.max_retries + 1)
        raise
    finally:
        session.close()


def apply_ats_mapping(data):
    """Replace OneXVoice agent ids with ATS employee codes.

    Fails the run rather than returning raw numeric ids: those would sail
    through combine.py and be rejected by HRMS one agent at a time, which is a
    far harder failure to read than a missing file.
    """
    if not os.path.exists(MAPPING_FILE):
        raise FileNotFoundError(
            f"Agent-id to ATS-code mapping not found: {ctx.relative(MAPPING_FILE)}. "
            "Without it every EmpCode stays a OneXVoice id and HRMS rejects the "
            "upload. Set DMI_ATS_MAPPING in .env or place the file there."
        )
    mapping_df = pd.read_excel(MAPPING_FILE)
    missing = [c for c in ("Agent Id", "ATS ID") if c not in mapping_df.columns]
    if missing:
        raise ValueError(
            f"{os.path.basename(MAPPING_FILE)} is missing column(s): "
            f"{', '.join(missing)}; got {list(mapping_df.columns)}"
        )

    mapping_df["Agent Id"] = mapping_df["Agent Id"].astype(str).str.strip()
    mapping_df["ATS ID"] = mapping_df["ATS ID"].astype(str).str.strip()
    mapping = dict(zip(mapping_df["Agent Id"], mapping_df["ATS ID"]))

    data["EmpCode"] = data["EmpCode"].astype(str).str.strip()
    before = data["EmpCode"].copy()
    data["EmpCode"] = data["EmpCode"].map(mapping).fillna(data["EmpCode"])
    unmapped = int((data["EmpCode"] == before).sum())
    if unmapped:
        ctx.warn(f"[{LEG_SUFFIX}] {unmapped} agent id(s) had no ATS code in "
                 f"{os.path.basename(MAPPING_FILE)}; left unchanged")
    return data


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
        raw_data.to_csv(os.path.join(csv_data_dir, new_csv_name), index=False)
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

        # OneXVoice names these differently from Smart Dial: "Total Breaks"
        # rather than "Total Break Duration", "Agent Id" rather than "Agent ID".
        if "Login Duration" in data.columns and "Total Breaks" in data.columns:
            data["Login Duration (minutes)"] = data["Login Duration"].apply(time_to_minutes)
            data["Total Breaks (minutes)"] = data["Total Breaks"].apply(time_to_minutes)

            data["Minutes"] = (data["Login Duration (minutes)"] - data["Total Breaks (minutes)"])

            # OneXVoice reports Login Duration as 00:00:00 for a sizeable
            # share of real sessions even though it records both Login and
            # Logout. Taking the column at face value drops those agents from
            # the report entirely, which shows up in HRMS as a day of absence
            # for someone who worked a full shift. Where the span is available
            # and the duration is not, use the span.
            span_minutes = (
                (pd.to_datetime(data["Logout"], errors="coerce")
                 - pd.to_datetime(data["Login"], errors="coerce"))
                .dt.total_seconds() / 60
            )
            recoverable = (data["Minutes"] <= 0) & span_minutes.gt(0).fillna(False)
            if recoverable.any():
                data.loc[recoverable, "Minutes"] = (
                    span_minutes[recoverable] - data.loc[recoverable, "Total Breaks (minutes)"]
                )
                ctx.warn(f"[{LEG_SUFFIX}] {int(recoverable.sum())} session(s) had no "
                         "Login Duration; minutes taken from Logout - Login instead")

            # An agent who never logged in has nothing to report.
            data = data[data["Minutes"] > 0]

            data["Agent Id"] = data["Agent Id"].fillna("").astype(str).str.strip()

            total_row_index = data[data["Agent Id"].str.contains('Total', case=False, na=False)].index
            if len(total_row_index) > 0:
                # .iloc would treat this label as a position; after the filters
                # above the two no longer agree, so the cut lands in the wrong
                # place. Select by label instead.
                data = data[data.index < total_row_index[0]]

            data = data[["Agent Id", "Login", "Minutes"]]
            data.rename(columns={"Agent Id": "EmpCode", "Login": "Date"}, inplace=True)

            data["EmpCode"] = (data["EmpCode"].fillna("").astype(str)
                               .str.replace(r"\s*\(.*\)", "", regex=True)
                               .str.upper()
                               # Ids arrive from pandas as floats ("123.0").
                               .str.replace(".0", "", regex=False)
                               .str.strip())
            data = data[data["EmpCode"].str.match(r"^\d+$", na=False)]

            data['Date'] = pd.to_datetime(data['Date'], errors='coerce').dt.strftime('%m-%d-%Y')
            data["Minutes"] = np.ceil(data["Minutes"]).astype(int)
            data["IsWH"] = "N"
            data = data[["EmpCode", "Date", "Minutes", "IsWH"]]

            data = apply_ats_mapping(data)
            data.to_csv(new_csv_path, index=False)
            ctx.set_records(scraped=len(data))
            ctx.step(f"[{LEG_SUFFIX}] Final rows: {len(data)}", stage=Stage.PROCESSING)
        else:
            ctx.fail("Required columns ('Login Duration' and 'Total Breaks') "
                     f"are missing; got {list(data.columns)[:12]}")
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
