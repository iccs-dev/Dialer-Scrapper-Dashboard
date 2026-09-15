"""Build the HRMS upload file, drive the upload, and mail the daily report.

Pipeline for one date:

1. collect every process's processed output
   ``Media/<process>/dialer_data/<YYYY>/<MM>/<DD>/<date>_APR.csv``;
2. tag each row with its process, total the minutes per agent and keep only
   ``ATS<digits>`` employee codes;
3. write ``Media/hrms/upload/<YYYY>/<MM>/<DD>/<date>.csv``;
4. run ``hrms.py`` to upload it;
5. if HRMS rejected agents, annotate
   ``Media/hrms/Failed_Agent_list/<YYYY>/<MM>/<DD>/<date>.csv``, drop those
   agents from the upload file and run ``hrms.py`` again with the clean list;
6. email the dialer-data report and the failed-agent report.

    venv/bin/python combine.py                    # yesterday
    venv/bin/python combine.py 2026-09-06         # a specific date
    venv/bin/python combine.py --no-email         # skip the reports
    venv/bin/python combine.py --dry-run          # no upload, no email
"""

import argparse
import logging
import mimetypes
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from core.runlog import Stage, start_run

import pandas as pd

common.load_env()

# ==================== CONFIGURATION ====================

#: Processes to report on. Unset means "every process in the media tree", so a
#: new scraper is picked up automatically. Set COMBINE_PROCESSES to a
#: comma-separated list to pin the report (processes with no data then show as
#: 0 agents instead of being omitted).
_configured = os.getenv("COMBINE_PROCESSES", "").strip()
CONFIGURED_PROCESSES = [p.strip() for p in _configured.split(",") if p.strip()]

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))


def _recipients(name, default=""):
    raw = os.getenv(name, default)
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


DIALER_DATA_RECIPIENTS = _recipients("DIALER_REPORT_RECIPIENTS")
FAILED_AGENT_RECIPIENTS = _recipients("FAILED_AGENT_RECIPIENTS")

#: Columns of the combined upload file, in order.
UPLOAD_COLUMNS = ["EmpCode", "Date", "Process", "IsWH", "Minutes"]
#: Columns of the annotated failed-agent file, in order.
FAILED_COLUMNS = ["Error", "EmpCode", "Date", "Minutes", "IsWH", "Process"]
#: Employee codes HRMS accepts.
EMPCODE_PATTERN = r"(?i)^ATS\d+$"

#: The four columns hrms.py writes into the raw failed-agent file.
RAW_FAILED_COLUMNS = ["Error", "EmpCode", "Date", "Minutes"]


def read_failed_agents(path):
    """Read the rejected-agent list as the four raw columns.

    hrms.py writes it headerless with four columns, but this script annotates
    it in place with IsWH and Process and a header row. Re-running for the same
    date therefore reads back a six-column file with headers, so both shapes
    have to be accepted - otherwise the second run dies with
    "Length mismatch: Expected axis has 6 elements, new values have 4".
    """
    try:
        first = ""
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
        if not first:
            return None
        if first.split(",")[:2] == RAW_FAILED_COLUMNS[:2]:
            # Already annotated by an earlier run: keep only the raw columns
            # so the annotation below behaves exactly as it does on a fresh file.
            frame = pd.read_csv(path)
            missing = [c for c in RAW_FAILED_COLUMNS if c not in frame.columns]
            if missing:
                return None
            return frame[RAW_FAILED_COLUMNS].copy()
        frame = pd.read_csv(path, header=None)
        if frame.shape[1] < len(RAW_FAILED_COLUMNS):
            return None
        frame = frame.iloc[:, : len(RAW_FAILED_COLUMNS)]
        frame.columns = RAW_FAILED_COLUMNS
        return frame
    except Exception:
        return None


# ==================== SETUP ====================

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("date", nargs="?", help="report date as YYYY-MM-DD (default: yesterday)")
parser.add_argument("--no-upload", action="store_true", help="build the file but do not run hrms.py")
parser.add_argument("--no-email", action="store_true", help="do not send the report emails")
parser.add_argument("--dry-run", action="store_true", help="implies --no-upload and --no-email")
args = parser.parse_args()

skip_upload = args.no_upload or args.dry_run
skip_email = args.no_email or args.dry_run

if args.date:
    _target_date = common.as_date(args.date)
else:
    _target_date = common.as_date(datetime.today() - timedelta(days=1))

def _processes_with_data(date):
    """Processes that actually produced a CSV for this date."""
    found = []
    for process in (CONFIGURED_PROCESSES or common.list_data_processes()):
        folder = common.ProcessPaths(process, date).dataset(
            common.FOLDER_PROCESSED, create=False
        )
        if os.path.isdir(folder) and any(
            name.lower().endswith(".csv") for name in os.listdir(folder)
        ):
            found.append(process)
    return found


# COMBINE lines go into the daily log of each process this run actually
# combines. A process whose scraper failed contributed nothing, so its log
# keeps "COMBINE : NOT RUN - Scraper failed" rather than claiming combine
# succeeded for it. If nothing produced data at all, fall back to every known
# process so the failure is still visible somewhere.
_daily_targets = (_processes_with_data(_target_date)
                  or CONFIGURED_PROCESSES or common.list_data_processes())
ctx = start_run(__file__, _target_date, daily_processes=_daily_targets)
paths = ctx.paths
upload_paths = common.ProcessPaths(common.UPLOAD_PROCESS, _target_date)

date_now = paths.date_key                                  # YYYY-MM-DD
display_date = _target_date.strftime("%m-%d-%Y")           # as HRMS returns it


def log(message, level=logging.INFO, only=None):
    """Structured, per-run log line (see core/runlog.py).

    `only` restricts the line to one process's daily log. Use it for anything
    that states a fact about a single process; without it the line is copied
    into every contributing process's file, which is how "ICAI: no data" ended
    up in Imagine's log.
    """
    text = str(message).replace("\n", " ")
    if level >= logging.ERROR:
        ctx.error(text, stage=Stage.PROCESSING, only=only)
    elif level >= logging.WARNING:
        ctx.warning(text, stage=Stage.PROCESSING, only=only)
    else:
        ctx.info(text, stage=Stage.PROCESSING, only=only)


# ==================== COLLECT DIALER DATA ====================

def processes_to_report():
    """Processes for the summary table, config-driven or discovered.

    A discovered process is one that owns a processed-output folder. That
    excludes the media folders scripts create just to hold their own logs
    (this script's own, and the upload process's).
    """
    if CONFIGURED_PROCESSES:
        return CONFIGURED_PROCESSES
    return [p for p in common.list_data_processes()
            if p not in (common.UPLOAD_PROCESS, paths.process)]


def collect(summary_rows):
    """Read each process's processed CSV, tagged with its process name."""
    frames = []
    for process in processes_to_report():
        folder = common.ProcessPaths(process, _target_date).dataset(
            common.FOLDER_PROCESSED, create=False
        )
        file_path = os.path.join(folder, upload_paths.report_name("csv"))

        total_agents = 0
        if os.path.exists(file_path):
            try:
                frame = pd.read_csv(file_path)
            except Exception as exc:
                log(f"{process}: could not read its CSV | Reason: {exc}", logging.ERROR,
                    only=process)
                frame = None
            if frame is not None:
                frame["Process"] = process
                for col in frame.select_dtypes(include=["datetime64"]):
                    frame[col] = frame[col].dt.strftime("%m-%d-%Y")
                frame["IsWH"] = "N"
                total_agents = len(frame)
                frames.append(frame)
                log(f"{process}: {total_agents} agents", only=process)
        else:
            log(f"{process}: no data", only=process)

        existing = next((r for r in summary_rows if r["Process"] == process), None)
        if existing:
            existing["Total agents"] += total_agents
        else:
            summary_rows.append({
                "Process": process,
                "Total agents": total_agents,
                "Agents uploaded": 0,
                "Failed": 0,
            })
    return frames


# ==================== HRMS ====================

def run_hrms(reason):
    """Invoke hrms.py for this date and return its stdout."""
    script = os.path.join(common.CODE_ROOT, "hrms.py")
    ctx.info(f"Running hrms.py ({reason})", stage=Stage.HRMS_UPLOAD)
    result = subprocess.run(
        [sys.executable, script, date_now],
        capture_output=True,
        text=True,
        cwd=common.CODE_ROOT,
    )
    # hrms.py writes its own [HRMS] block into the daily log of each process
    # in the upload, and routes per-process lines to the process they name.
    # Echoing its stdout here as COMBINE lines duplicated all of that AND
    # bypassed that routing - which is how "Imagine: 25 agent(s)" ended up in
    # DMI's and TN CM's logs. Keep it at debug: still on the console and in a
    # LOG_LEVEL=DEBUG run, out of the daily log.
    for line in (result.stdout or "").splitlines():
        if line.strip():
            ctx.detail(f"  hrms.py: {line.strip()}")
    if result.returncode != 0:
        log(f"hrms.py exited with code {result.returncode}", logging.WARNING)
    return result.stdout or ""


# ==================== EMAIL ====================

def send_email(recipients, subject, html_body, attachment):
    if not recipients:
        ctx.warn(f"No recipients configured for '{subject}'; email skipped")
        return
    sender_email = os.getenv("EMAIL_SENDER")
    sender_password = os.getenv("EMAIL_PASSWORD")
    if not sender_email or not sender_password:
        ctx.warn("EMAIL_SENDER/EMAIL_PASSWORD not set; email skipped")
        return

    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content("This is an HTML email. Please view it in an HTML-compatible email client.")
    msg.add_alternative(html_body, subtype="html")

    with open(attachment, "rb") as handle:
        file_data = handle.read()
    file_type = mimetypes.guess_type(attachment)[0] or "application/octet-stream"
    maintype, _, subtype = file_type.partition("/")
    msg.add_attachment(
        file_data, maintype=maintype, subtype=subtype or "octet-stream",
        filename=os.path.basename(attachment),
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
        log(f"Report emailed to {', '.join(recipients)}")
    except Exception as exc:
        ctx.fail(f"Could not email '{subject}' | Reason: {exc}")


def build_summary_html(summary_rows):
    total_agents = sum(row["Total agents"] for row in summary_rows)
    total_uploaded = sum(row["Agents uploaded"] for row in summary_rows)
    total_failed = sum(row["Failed"] for row in summary_rows)

    rows_html = "".join(
        f"""
            <tr>
                <td>{row['Process']}</td>
                <td>{row['Total agents']}</td>
                <td>{row['Agents uploaded']}</td>
                <td>{row['Failed']}</td>
            </tr>"""
        for row in summary_rows
    )

    return f"""
    <html>
    <body>
    <p>Please find the attached dialer data for {date_now}.</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; font-family: Arial, sans-serif;">
        <thead style="background-color: #f2f2f2;">
            <tr>
                <th>Process</th>
                <th>Total agents</th>
                <th>Agents uploaded</th>
                <th>Failed</th>
            </tr>
        </thead>
        <tbody>{rows_html}
        <tr style="font-weight: bold; background-color: #d9edf7;">
            <td>Total</td>
            <td>{total_agents}</td>
            <td>{total_uploaded}</td>
            <td>{total_failed}</td>
        </tr>
        </tbody>
    </table>
    </body>
    </html>
    """


# ==================== MAIN ====================

def main():
    summary_rows = []

    frames = collect(summary_rows)
    if not frames:
        ctx.fail("No dialer data found to combine")
        ctx.failed()
        return 1

    combined_before_filtering = pd.concat(frames, ignore_index=True)

    combined = combined_before_filtering.groupby(
        ["EmpCode", "Date", "Process", "IsWH"], as_index=False
    ).agg({"Minutes": "sum"})
    combined = combined[combined["EmpCode"].str.match(EMPCODE_PATTERN, na=False)]
    combined = combined[UPLOAD_COLUMNS]

    combined_path = upload_paths.file(common.FOLDER_UPLOAD, upload_paths.dated_name("csv"))
    combined.to_csv(combined_path, index=False)
    log(f"Upload file: {os.path.basename(combined_path)} | {len(combined)} rows")
    ctx.detail(f"wrote {ctx.relative(combined_path)}")

    if skip_upload:
        log("Upload skipped (--no-upload/--dry-run)")
    else:
        run_hrms("initial upload")

    # ---- failed agents ----
    failed_path = upload_paths.file(
        common.FOLDER_FAILED_AGENTS, upload_paths.dated_name("csv"), create=False
    )
    had_failures = False

    if os.path.exists(failed_path):
        failed = read_failed_agents(failed_path)
    else:
        failed = None

    if failed is not None and not failed.empty:
        failed["IsWH"] = "N"
        failed["Date"] = display_date

        mapping = combined_before_filtering[["EmpCode", "Process"]].drop_duplicates()
        failed = failed.merge(mapping, on="EmpCode", how="left")
        failed["Process"] = failed["Process"].fillna("Unknown")

        failed_counts = failed["Process"].value_counts().to_dict()
        for row in summary_rows:
            row["Failed"] = failed_counts.get(row["Process"], 0)

        failed = failed[FAILED_COLUMNS]
        failed.to_csv(failed_path, index=False)
        log(f"Rejected agents: {len(failed)}")
        ctx.detail(f"annotated {ctx.relative(failed_path)}")

        # Drop the rejected agents and retry, matching hrms.py's
        # "will retry with clean list" behaviour.
        failed_codes = failed["EmpCode"].astype(str).unique()
        combined = pd.read_csv(combined_path)
        before = len(combined)
        combined = combined[~combined["EmpCode"].astype(str).isin(failed_codes)]
        combined.to_csv(combined_path, index=False)
        had_failures = before != len(combined)
        log(f"Removed {before - len(combined)} rejected agent(s) from the upload file")

        upload_counts = combined["Process"].value_counts().to_dict()
        for row in summary_rows:
            row["Agents uploaded"] = upload_counts.get(row["Process"], 0)

        if len(combined) == 0:
            # Every agent was rejected, so the "clean list" is just a header.
            # Re-running hrms.py on it uploads nothing and fails with
            # "Could not locate Save Excel button", which looks like a UI
            # change rather than what actually happened.
            reasons = common.summarise_reasons(
                (str(row.get("Error") or "Rejected"), str(row.get("EmpCode")))
                for _, row in failed.iterrows()
            )
            ctx.fail(f"All {before} agent(s) rejected by HRMS; nothing left to upload")
            ctx.fail(f"Reason: {reasons}")
            ctx.detail("retry skipped: upload file would contain only its header")
        elif had_failures and not skip_upload:
            run_hrms("retry with clean list")
    else:
        log("No agents rejected by HRMS")
        upload_counts = combined["Process"].value_counts().to_dict()
        for row in summary_rows:
            row["Agents uploaded"] = upload_counts.get(row["Process"], 0)

    # ---- reports ----
    if skip_email:
        log("Emails skipped (--no-email/--dry-run)")
    else:
        send_email(
            DIALER_DATA_RECIPIENTS,
            f"Dialer Data Report - {date_now}",
            build_summary_html(summary_rows),
            combined_path,
        )
        if os.path.exists(failed_path):
            send_email(
                FAILED_AGENT_RECIPIENTS,
                f"Failed Agents Report - {date_now}",
                "<html><body><p>Please find the attached failed agent list "
                f"for {date_now}.</p></body></html>",
                failed_path,
            )
        else:
            ctx.detail("No rejected agents; failed-agent email skipped")

    # The exit code has to agree with the closing line, or cron and
    # run_scrapers would treat a failed combine as a success.
    if ctx.error_count:
        ctx.failed(f"{ctx.process} ({ctx.error_count} error(s))")
        return 1
    ctx.success(f"Completed in {ctx.elapsed_seconds:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
