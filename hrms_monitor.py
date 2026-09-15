"""Daily scraper / HRMS status report.

Role change: this used to *discover* failures after the fact by scraping
free-text logs, guessing at exception meanings and parsing scrape.ps1. The
scrapers now record their own structured logs and error history, so the monitor
reads those instead of reverse-engineering them:

    structured logs  (Media/<process>/logs/<Y>/<M>/<D>/<ts>_<run>.log)
    + error history  (Media/<process>/errors/<Y>/<M>/<D>/history.jsonl)
    + HRMS results   (upload file, Failed_Agent_list, hrms run logs)
            |
        hrms_monitor.py
            |
        daily status report  (HTML + CSV + email)

It stays a pure observer: it imports common/core for paths and vocabulary,
writes only to Media/Monitor/, and can be re-run for any past date.

Reused from the previous version: the report shape, the HTML style and table
rendering, summarise_reasons(), write_outputs(), send_email(), subject_for()
and the "a monitor that dies silently is worse than no monitor" fallback.
Dropped: the scrape.ps1 log parser, the combine.py/dashboard.py AST readers and
the Selenium exception-guessing tables - the structured logs now carry stage
and category directly, and core.errors.HUMAN_CAUSE turns a category into a
sentence.

    python hrms_monitor.py                  # yesterday
    python hrms_monitor.py 2026-09-05
    python hrms_monitor.py 2026-09-05 --no-email
"""

import argparse
import csv as _csv
import html
import json
import os
import re
import smtplib
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
from core.config import settings
from core.errors import HUMAN_CAUSE
from core.runlog import LOG_DATEFMT, Stage

OUT_DIR_NAME = "Monitor"

#: Processes to leave out of the report. Excluding one hides real failures, so
#: keep the reason next to the name.
EXCLUDED_PROCESSES = set()

#: `timestamp | level | process | run_id | target_date | stage | message`
LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| "
    r"(?P<level>\w+)\s* \| (?P<process>[^|]+?)\s* \| (?P<run_id>[^|]+?)\s* \| "
    r"(?P<date>[^|]+?)\s* \| (?P<stage>[^|]+?)\s* \| (?P<message>.*)$"
)

RUN_DONE = re.compile(r"^Run (completed|failed|no-save)")


# ---------------------------------------------------------------------------
# Reading what the pipeline recorded
# ---------------------------------------------------------------------------

class Run:
    """One scraper invocation, reconstructed from its structured log."""

    def __init__(self, process, run_id, path):
        self.process = process
        self.run_id = run_id
        self.path = Path(path)
        self.started = None
        self.finished = None
        self.stages = []
        self.errors = []
        self.warnings = []
        self.status = "incomplete"
        self.messages = []

    @property
    def last_stage(self):
        return self.stages[-1] if self.stages else Stage.STARTUP

    @property
    def duration(self):
        if self.started and self.finished:
            return (self.finished - self.started).total_seconds()
        return None

    def __repr__(self):
        return f"Run({self.process}/{self.run_id}: {self.status} @ {self.last_stage})"


def parse_run_logs(process, date_str):
    """Every run of *process* on *date_str*, newest last."""
    paths = common.ProcessPaths(process, date_str)
    log_dir = Path(paths.logs(create=False))
    if not log_dir.is_dir():
        return []

    runs = {}
    for path in sorted(log_dir.glob("*.log")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = LOG_LINE.match(line)
            if not match:
                continue  # traceback continuation lines
            fields = match.groupdict()
            run_id = fields["run_id"].strip()
            run = runs.get(run_id)
            if run is None:
                run = runs[run_id] = Run(fields["process"].strip(), run_id, path)
            stamp = datetime.strptime(fields["ts"], LOG_DATEFMT)
            run.started = run.started or stamp
            run.finished = stamp

            stage = fields["stage"].strip()
            if stage not in run.stages:
                run.stages.append(stage)

            message = fields["message"].strip()
            level = fields["level"].strip()
            if level in ("ERROR", "CRITICAL"):
                run.errors.append((stage, message))
            elif level == "WARNING":
                run.warnings.append((stage, message))

            done = RUN_DONE.match(message)
            if done:
                run.status = "success" if done.group(1) == "completed" else done.group(1)
            run.messages.append((stage, level, message))

    ordered = sorted(runs.values(), key=lambda r: r.started or datetime.min)
    for run in ordered:
        if run.status == "incomplete" and run.errors:
            run.status = "failed"
    return ordered


#: A detail line in the daily log: "[04:37:06] [COMBINE] PROCESSING - ..."
DAILY_DETAIL = re.compile(
    r"^\[(?P<time>\d{2}:\d{2}:\d{2})\] \[(?P<stage>[A-Z]+)\] (?P<message>.*)$"
)
#: A skipped stage, which carries no timestamp.
DAILY_SKIPPED = re.compile(r"^\[(?P<stage>[A-Z]+)\] NOT RUN - (?P<reason>.*)$")
DAILY_STATUS = re.compile(r"^(?P<stage>SCRAPER|COMBINE|HRMS)\s*:\s*(?P<status>.+?)\s*$")


class DailyStatus:
    """One day of one process, read back from its combined daily log."""

    def __init__(self, process, path):
        self.process = process
        self.path = path
        self.statuses = {s: "NOT RUN" for s in ("SCRAPER", "COMBINE", "HRMS")}
        self.errors = []      # (stage, message)
        self.warnings = []    # (stage, message)
        self.skipped = []     # (stage, reason)
        self.runs = 0
        self.stages_seen = []

    @property
    def failed_stages(self):
        return [s for s, v in self.statuses.items() if v == "FAILED"]

    @property
    def status(self):
        if self.failed_stages:
            return "failed"
        if self.statuses["SCRAPER"] == "SUCCESS":
            return "success"
        return "incomplete"

    @property
    def last_stage(self):
        return self.stages_seen[-1] if self.stages_seen else "-"

    def summary(self):
        return " | ".join(f"{s[0]}:{self.statuses[s]}" for s in
                          ("SCRAPER", "COMBINE", "HRMS"))


def read_daily_log(process, date_str):
    """Parse Media/<process>/APR_Logs/Y/M/D/<process>_<date>.log, or None.

    The APR tree is the one that carries the HRMS upload; Disposition logs are
    a separate report and are not part of this workflow.
    """
    paths = common.ProcessPaths(process, date_str)
    path = (Path(paths.dataset(common.FOLDER_APR_LOGS, create=False))
            / f"{process}_{date_str}.log")
    if not path.is_file():
        # Written before APR and Disposition logs were split.
        path = (Path(paths.dataset(common.FOLDER_LOGS, create=False))
                / f"{process}_{date_str}.log")
    if not path.is_file():
        return None

    daily = DailyStatus(process, path)
    text = path.read_text(encoding="utf-8", errors="replace")
    head, _, details = text.partition("DETAILS")
    for line in head.splitlines():
        found = DAILY_STATUS.match(line)
        if found:
            daily.statuses[found.group("stage")] = found.group("status")

    for line in details.splitlines():
        skipped = DAILY_SKIPPED.match(line.strip())
        if skipped:
            daily.skipped.append((skipped.group("stage"), skipped.group("reason")))
            continue
        detail = DAILY_DETAIL.match(line)
        if not detail:
            continue
        stage, message = detail.group("stage"), detail.group("message").strip()
        if stage not in daily.stages_seen:
            daily.stages_seen.append(stage)
        if message == "START" and stage == "SCRAPER":
            daily.runs += 1
        if message.startswith("ERROR - "):
            daily.errors.append((stage, message[len("ERROR - "):]))
        elif message.startswith("WARN - "):
            daily.warnings.append((stage, message[len("WARN - "):]))
    return daily


def read_error_history(process, date_str):
    """Structured failure records this process wrote for the date."""
    paths = common.ProcessPaths(process, date_str)
    history = Path(paths.dataset(common.FOLDER_ERRORS, create=False)) / "history.jsonl"
    if not history.is_file():
        return []
    records = []
    for line in history.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def read_upload_counts(date_str):
    """{process: agents} from the file that was sent to HRMS."""
    paths = common.ProcessPaths(common.UPLOAD_PROCESS, date_str)
    path = Path(paths.file(common.FOLDER_UPLOAD, paths.dated_name("csv"), create=False))
    if not path.is_file():
        return None
    import pandas as pd

    try:
        frame = pd.read_csv(path)
    except Exception:
        return None
    if "Process" in frame.columns:
        return {str(k): int(v) for k, v in frame["Process"].value_counts().items()}
    return {"(unattributed)": len(frame)}


def read_failed_agents(date_str):
    """{process: [(reason, empcode)]} from Failed_Agent_list."""
    paths = common.ProcessPaths(common.UPLOAD_PROCESS, date_str)
    path = Path(paths.file(common.FOLDER_FAILED_AGENTS, paths.dated_name("csv"),
                           create=False))
    if not path.is_file():
        return {}, []
    import pandas as pd

    try:
        frame = pd.read_csv(path)
    except Exception:
        return {}, []

    by_process = defaultdict(list)
    unassigned = []
    for _, row in frame.iterrows():
        reason = str(row.get("Error", "") or "Rejected").strip()
        emp = str(row.get("EmpCode", "") or "?").strip()
        process = str(row.get("Process", "") or "").strip()
        if process and process.lower() != "unknown":
            by_process[process].append((reason, emp))
        else:
            unassigned.append((reason, emp))
    return dict(by_process), unassigned


def scraped_rows(process, date_str):
    """How many processed rows the scraper produced, or None if it produced none."""
    paths = common.ProcessPaths(process, date_str)
    folder = Path(paths.dataset(common.FOLDER_PROCESSED, create=False))
    if not folder.is_dir():
        return None
    total = 0
    found = False
    for path in folder.glob("*.csv"):
        found = True
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                total += max(0, sum(1 for _ in handle) - 1)
        except OSError:
            pass
    return total if found else None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def one_line(text, limit=160):
    """Collapse a multi-line message into one readable table cell."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def summarise_reasons(entries, limit=2):
    """'3 EmpCode Not found (ATS1, ATS2 +1); 1 Not Active (ATS9)'"""
    grouped = defaultdict(list)
    for reason, emp in entries:
        grouped[reason or "Rejected"].append(emp)
    parts = []
    for reason, emps in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        sample = ", ".join(emps[:limit])
        if len(emps) > limit:
            sample += f" +{len(emps) - limit}"
        parts.append(f"{len(emps)} {reason} ({sample})")
    return "; ".join(parts)


def describe_failure(run, errors):
    """One actionable sentence for a failed run."""
    if errors:
        latest = errors[-1]
        cause = HUMAN_CAUSE.get(latest.get("category"), "Unexpected error")
        return (f"{cause} during {latest.get('stage')} "
                f"({latest.get('exception_type')}: "
                f"{(latest.get('exception_message') or '')[:120]})")
    if run and run.errors:
        stage, message = run.errors[-1]
        return f"Failed during {stage}: {message[:160]}"
    if run:
        return f"Run did not complete; last stage reached was {run.last_stage}"
    return "No run recorded for this date"


def build_report(date_str):
    processes = [p for p in common.list_data_processes()
                 if p not in EXCLUDED_PROCESSES and p != common.UPLOAD_PROCESS]

    upload_counts = read_upload_counts(date_str)
    upload_missing = upload_counts is None
    upload_counts = upload_counts or {}
    failed_by_process, unassigned = read_failed_agents(date_str)

    rows = []
    all_errors = []
    saved_anywhere = False

    for process in processes:
        daily = read_daily_log(process, date_str)
        errors = read_error_history(process, date_str)
        all_errors.extend(errors)
        produced = scraped_rows(process, date_str)
        contributed = int(upload_counts.get(process, 0))
        rejected = failed_by_process.get(process, [])

        if daily is None:
            # Either nothing ran, or the date predates the combined daily log,
            # in which case the old per-run logs still answer for it.
            legacy = parse_run_logs(process, date_str)
            rows.append({
                "Process": process, "Status": "Success" if legacy else "Failed",
                "Runs": len(legacy),
                "Scraper": "-", "Combine": "-", "Hrms": "-",
                "Rows": produced or 0, "Agents": contributed,
                "Issue": ("Logged before the combined daily log" if legacy
                          else "No run recorded for this date"),
            })
            continue

        if daily.statuses["HRMS"] == "SUCCESS":
            saved_anywhere = True

        issues, notes = [], []
        for stage in ("SCRAPER", "COMBINE", "HRMS"):
            if daily.statuses[stage] == "FAILED":
                reason = next((m for st, m in daily.errors if st == stage),
                              "no reason logged")
                issues.append(f"{stage} failed: {one_line(reason, 120)}")
        for stage, reason in daily.skipped:
            notes.append(f"{stage} skipped: {reason}")
        if not issues and daily.statuses["SCRAPER"] == "SUCCESS" and not produced:
            issues.append("Scraper completed but produced no rows")
        if not issues and produced and contributed == 0:
            notes.append("Upload file not found" if upload_missing
                         else "No valid ATS EmpCodes reached the upload file")
        if rejected:
            notes.append(f"{len(rejected)} agents rejected: {summarise_reasons(rejected)}")
        drift = [m for _st, m in daily.warnings if "fallback locator" in m]
        if drift:
            notes.append(f"{len(drift)} locator fallback(s) used - UI may have changed")

        rows.append({
            "Process": process,
            "Status": "Failed" if issues else "Success",
            "Runs": daily.runs,
            "Scraper": daily.statuses["SCRAPER"],
            "Combine": daily.statuses["COMBINE"],
            "Hrms": daily.statuses["HRMS"],
            "Rows": produced if produced is not None else 0,
            "Agents": contributed,
            "Issue": " | ".join(issues + notes) if (issues or notes) else "No Issue",
        })

    return {
        "date": date_str,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": rows,
        "errors": all_errors + read_error_history(common.UPLOAD_PROCESS, date_str),
        "hrms_saved": saved_anywhere,
        "hrms_issue": None if saved_anywhere else "HRMS upload not confirmed",
        "upload_missing": upload_missing,
        "unassigned": unassigned,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

STYLE = """
body { font-family: Segoe UI, Arial, sans-serif; font-size: 13px; color: #1a1a1a; }
h2 { margin: 18px 0 4px; font-size: 16px; }
p.sub { margin: 0 0 12px; color: #555; }
table { border-collapse: collapse; width: 100%; margin-bottom: 22px; }
th { background: #263238; color: #fff; text-align: left; padding: 7px 9px;
     font-weight: 600; font-size: 12px; }
td { border-bottom: 1px solid #e0e0e0; padding: 6px 9px; vertical-align: top; }
tr:nth-child(even) td { background: #fafafa; }
.ok   { color: #1b5e20; font-weight: 600; }
.bad  { color: #b71c1c; font-weight: 600; }
.na   { color: #616161; font-weight: 600; }
.issue-ok   { color: #777; }
.issue-bad  { color: #b71c1c; }
.issue-note { color: #8a6d00; }
.banner { padding: 10px 12px; border-radius: 4px; margin-bottom: 16px; font-weight: 600; }
.banner-ok  { background: #e8f5e9; color: #1b5e20; border-left: 4px solid #2e7d32; }
.banner-bad { background: #ffebee; color: #b71c1c; border-left: 4px solid #c62828; }
code { font-size: 11px; color: #444; }
"""


def _row_html(row):
    cls = {"Success": "ok", "Failed": "bad"}.get(row["Status"], "na")
    if row["Issue"] == "No Issue":
        issue_cls = "issue-ok"
    elif row["Status"] == "Failed":
        issue_cls = "issue-bad"
    else:
        issue_cls = "issue-note"
    return (
        "<tr>"
        f"<td>{html.escape(str(row['Process']))}</td>"
        f"<td class='{cls}'>{html.escape(row['Status'])}</td>"
        f"<td>{row['Runs']}</td>"
        f"<td>{html.escape(str(row['Scraper']))}</td>"
        f"<td>{html.escape(str(row['Combine']))}</td>"
        f"<td>{html.escape(str(row['Hrms']))}</td>"
        f"<td>{row['Rows']}</td>"
        f"<td>{row['Agents']}</td>"
        f"<td class='{issue_cls}'>{html.escape(str(row['Issue']))}</td>"
        "</tr>"
    )


def _errors_html(errors):
    if not errors:
        return "<p class='sub'>No failures recorded.</p>"
    body = "".join(
        "<tr>"
        f"<td>{html.escape(str(e.get('process')))}</td>"
        f"<td>{html.escape(str(e.get('stage')))}</td>"
        f"<td>{html.escape(str(e.get('category')))}</td>"
        f"<td>{html.escape((str(e.get('exception_type')) + ': ' + str(e.get('exception_message')))[:160])}</td>"
        f"<td><code>{html.escape(str(e.get('log_path') or '-'))}</code><br>"
        f"<code>{html.escape(str(e.get('screenshot_path') or 'no screenshot'))}</code><br>"
        f"<code>{html.escape(str(e.get('trace_path') or 'no trace'))}</code></td>"
        "</tr>"
        for e in errors
    )
    return (
        "<table><thead><tr><th>Process</th><th>Stage</th><th>Category</th>"
        "<th>Error</th><th>Evidence</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def render_html(report):
    rows = report["rows"]
    failed = [r for r in rows if r["Status"] == "Failed"]

    if report["hrms_saved"] and not failed:
        banner = "<div class='banner banner-ok'>All processes completed and HRMS saved the upload.</div>"
    else:
        parts = []
        if not report["hrms_saved"]:
            parts.append(f"HRMS upload not confirmed - {html.escape(str(report['hrms_issue']))}")
        if failed:
            parts.append(f"{len(failed)} process(es) failed")
        banner = f"<div class='banner banner-bad'>{' | '.join(parts)}</div>"

    body = "".join(_row_html(r) for r in rows)
    return f"""<html><head><meta charset="utf-8"><style>{STYLE}</style></head><body>
<h2>Scraper / HRMS status - {html.escape(report['date'])}</h2>
<p class="sub">Generated {html.escape(report['generated'])} from structured logs and error history.</p>
{banner}
<table>
  <thead><tr><th>Process</th><th>Status</th><th>Runs</th><th>Scraper</th>
  <th>Combine</th><th>HRMS</th><th>Rows scraped</th><th>Agents uploaded</th>
  <th>Issue</th></tr></thead>
  <tbody>{body}</tbody>
</table>
<h2>Failures</h2>
{_errors_html(report['errors'])}
</body></html>"""


def write_outputs(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = report["date"]

    html_path = out_dir / f"{date_str}_status.html"
    html_path.write_text(render_html(report), encoding="utf-8")

    csv_path = out_dir / f"{date_str}_status.csv"
    # utf-8-sig so Excel opens it without mangling characters.
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = _csv.writer(handle)
        writer.writerow(["Process", "Status", "Runs", "Scraper", "Combine", "HRMS",
                         "Rows scraped", "Agents uploaded", "Issue"])
        for row in report["rows"]:
            writer.writerow([row["Process"], row["Status"], row["Runs"],
                             row["Scraper"], row["Combine"], row["Hrms"],
                             row["Rows"], row["Agents"], row["Issue"]])
    return html_path, csv_path


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def send_email(subject, body_html, recipients):
    if not recipients:
        print("[monitor] No recipients configured - not sending")
        return False
    sender = settings.email_sender
    password = settings.email_password
    if not sender or not password:
        print("[monitor] EMAIL_SENDER/EMAIL_PASSWORD not set - cannot send")
        return False

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content("This report requires an HTML-capable mail client.")
    message.add_alternative(body_html, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                          timeout=settings.smtp_timeout) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(message)
        print(f"[monitor] Email sent to {', '.join(recipients)}")
        return True
    except Exception as exc:
        print(f"[monitor] Failed to send email: {exc}")
        return False


def recipients():
    """MONITOR_EMAIL_TO, else the scraper alert list."""
    raw = os.getenv("MONITOR_EMAIL_TO", "")
    listed = [a.strip() for a in raw.split(",") if a.strip()]
    return listed or list(settings.error_email_to)


def subject_for(report):
    failed = [r for r in report["rows"] if r["Status"] == "Failed"]
    if not report["hrms_saved"]:
        state = "UPLOAD NOT CONFIRMED"
    elif failed:
        state = f"{len(failed)} ISSUE(S)"
    else:
        state = "OK"
    return f"[Scraper Status] {report['date']} - {state}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Daily scraper / HRMS status report")
    parser.add_argument("date", nargs="?", help="Target date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--no-email", action="store_true", help="Render only, do not send")
    parser.add_argument("--out", default=None, help="Output directory")
    args = parser.parse_args(argv)

    common.load_env()
    if args.date:
        try:
            date_str = common.as_date(args.date).strftime(common.DATE_FORMAT)
        except ValueError:
            print(f"[monitor] Invalid date '{args.date}', expected YYYY-MM-DD")
            return 2
    else:
        date_str = (datetime.now() - timedelta(days=1)).strftime(common.DATE_FORMAT)

    out_dir = args.out or common.media_path(OUT_DIR_NAME, create=True)

    try:
        report = build_report(date_str)
        html_path, csv_path = write_outputs(report, out_dir)
        failed = sum(1 for r in report["rows"] if r["Status"] == "Failed")
        print(f"[monitor] {date_str}: hrms_saved={report['hrms_saved']} "
              f"failed={failed}/{len(report['rows'])} errors={len(report['errors'])}")
        print(f"[monitor] Wrote {html_path}")
        print(f"[monitor] Wrote {csv_path}")
        if not args.no_email:
            send_email(subject_for(report), render_html(report), recipients())
        return 0
    except Exception:
        # A monitor that dies silently is worse than no monitor: report the
        # failure by email rather than only to a log nobody reads.
        detail = traceback.format_exc()
        print(f"[monitor] FAILED:\n{detail}")
        if not args.no_email:
            send_email(
                f"[Scraper Status] {date_str} - MONITOR FAILED",
                "<p>The status monitor failed to build its report.</p>"
                f"<pre>{html.escape(detail)}</pre>",
                recipients(),
             )
        return 1


if __name__ == "__main__":
    sys.exit(main())
