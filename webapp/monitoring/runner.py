"""Launches scraper scripts from the dashboard's Run button.

This is a port of the Streamlit dashboard's run_single_script/run_scraper,
with the same two behaviours preserved:

* range_aware processes get ["start", "end"] once; everything else is looped
  one date at a time (requirement 14 - intended behaviour preserved);
* success is verified by checking the expected output file actually appeared,
  not just by the exit code - a script can exit 0 having produced nothing;
* combine.py runs once after the scrapers for each date, exactly as the
  Streamlit run_scraper() did. combine.py builds the HRMS upload file and
  invokes hrms.py, so this single step is what pushes data to HRMS.

The subprocess is the existing, unmodified scraper. Nothing here reaches into
scraper logic (requirement 12); it only starts it and records what happened.
"""

import logging
import os
import subprocess
import sys
import threading
import uuid
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .models import Process, ProcessKind, ProcessLog, ProcessRun, Status

#: Runs after the scrapers for a date, builds the HRMS upload file and invokes
#: hrms.py itself. The Streamlit dashboard called this at the end of every
#: date; leaving it out is why nothing reached HRMS.
WORKFLOW_SCRIPT = "combine.py"

logger = logging.getLogger(__name__)

#: Guards against two dashboard clicks running the same process twice.
_active = set()
_lock = threading.Lock()


def is_running(process_name):
    with _lock:
        return process_name in _active


def active_processes():
    with _lock:
        return sorted(_active)


def _python():
    """The interpreter that runs the scrapers - this venv, not the system one."""
    return sys.executable


def _expected_output(process, run_date):
    """Where the script should leave its file, in the dated layout.

    Returns [] for a step that legitimately writes nothing - hrms.py uploads
    to a remote system and leaves no local file, so checking for one would
    mark every successful upload as a warning.
    """
    if process.kind == ProcessKind.WORKFLOW and not process.output_dir:
        return []
    parts = (process.output_dir or f"{process.name}/dialer_data").split("/")
    pattern = process.file_pattern or "{date}_APR.csv"
    folder = Path(settings.SCRAPER_ROOT) / "Media" / Path(*parts) / \
        f"{run_date.year:04d}" / f"{run_date.month:02d}" / f"{run_date.day:02d}"
    primary = folder / pattern.format(date=run_date.isoformat())
    siblings = [primary]
    if primary.suffix == ".csv":
        siblings.append(primary.with_suffix(".xlsx"))
    elif primary.suffix == ".xlsx":
        siblings.append(primary.with_suffix(".csv"))
    return siblings


def _log(run, message, level="INFO", stage=""):
    ProcessLog.objects.create(run=run, message=message[:4000], level=level, stage=stage)
    _notify(run, "log")


def _notify(run, reason):
    """Mirror runner-driven changes onto the WebSocket, same as the API does."""
    from .api import _broadcast
    _broadcast(run, reason=reason)


def _run_one(process, run_date, date_args, extra_env=None):
    """Run the script once and return True when it produced its output file."""
    root = Path(settings.SCRAPER_ROOT)
    script = root / process.script_path
    # The script reports to the monitoring API under this id too. Handing it
    # down means both sides write to one row; without it the runner's row and
    # the script's row were separate, and every dashboard run showed twice.
    run_id = uuid.uuid4().hex[:8]
    run = ProcessRun.objects.create(
        process=process, run_date=run_date, run_id=run_id, status=Status.RUNNING,
        started_at=timezone.now(), last_heartbeat=timezone.now(),
    )

    if not process.script_path or not script.is_file():
        run.status = Status.FAILED
        run.completed_at = timezone.now()
        run.error = f"script not found: {process.script_path or '(not configured)'}"
        run.save()
        _log(run, run.error, level="ERROR")
        _notify(run, "failed")
        return False

    command = [_python(), str(script)] + list(date_args)
    _notify(run, "started")
    _log(run, f"START {process.name} {' '.join(date_args) or '(yesterday)'}")

    try:
        completed = subprocess.run(
            command, cwd=str(script.parent), capture_output=True, text=True,
            timeout=settings.RUN_TIMEOUT_SECONDS,
            env={**os.environ, "DIALER_RUN_ID": run_id, **(extra_env or {})},
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        for line in output.splitlines():
            if line.strip():
                _log(run, line.rstrip()[:4000])
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        run.status = Status.FAILED
        run.completed_at = timezone.now()
        run.error = f"timed out after {settings.RUN_TIMEOUT_SECONDS}s"
        run.save()
        _log(run, run.error, level="ERROR")
        _notify(run, "failed")
        return False
    except OSError as exc:
        run.status = Status.FAILED
        run.completed_at = timezone.now()
        run.error = f"could not start: {exc}"
        run.save()
        _log(run, run.error, level="ERROR")
        _notify(run, "failed")
        return False

    # Verify the output file, exactly as the Streamlit version did.
    expected = _expected_output(process, run_date)
    produced = next((p for p in expected if p.is_file()), None)
    rows = 0
    if produced is not None:
        try:
            import pandas as pd
            frame = pd.read_csv(produced) if produced.suffix == ".csv" \
                else pd.read_excel(produced)
            rows = len(frame)
        except Exception:
            rows = -1

    run.completed_at = timezone.now()
    run.records_scraped = max(rows, 0)
    if returncode == 0 and (produced is not None or not expected):
        run.status = Status.SUCCESS
    elif returncode == 0:
        # Exit code says fine, but nothing landed - that is the "no data"
        # warning the old dashboard showed in amber.
        run.status = Status.WARNING
        run.error = "script finished but produced no output file"
    else:
        run.status = Status.FAILED
        run.error = f"exit code {returncode}"
    run.save()
    _log(run, f"{run.status} {process.name} rows={run.records_scraped}",
         level="ERROR" if run.status == Status.FAILED else "INFO")
    _notify(run, "finished")
    return run.status == Status.SUCCESS


def _run_parallel(processes, run_date, date_args):
    """Run these processes at the same time and wait for all of them.

    Waiting is the point: combine.py reads what the scrapers wrote, so it
    cannot start until every one of them has finished.
    """
    threads = []
    for process in processes:
        thread = threading.Thread(
            target=_run_one_safely, args=(process, run_date, date_args), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


def _run_one_safely(process, run_date, date_args, extra_env=None):
    """_run_one in a thread of its own: one failure must not stop the rest."""
    close_old_connections()
    try:
        _run_one(process, run_date, date_args, extra_env=extra_env)
    except Exception:                       # pragma: no cover - defensive
        logger.exception("runner crashed on %s for %s", process.name, run_date)
    finally:
        close_old_connections()


def _workflow_step():
    """The combine.py roster entry, or None if it was never registered."""
    return Process.objects.filter(
        script_name=WORKFLOW_SCRIPT, kind=ProcessKind.WORKFLOW).first()


def _orchestrate(scrapers, workflow, dates, names, include_workflow):
    """Scrapers for a date, then the workflow step for that date.

    This mirrors the Streamlit run_scraper() ordering. Range-aware processes
    still run once over the whole window first, because they log in once and
    handle the range themselves.
    """
    close_old_connections()
    try:
        range_aware = [p for p in scrapers if p.range_aware] if len(dates) > 1 else []
        per_date = [p for p in scrapers if p not in range_aware]

        if range_aware:
            _run_parallel(range_aware, dates[-1],
                          [dates[0].isoformat(), dates[-1].isoformat()])

        for run_date in dates:
            if per_date:
                _run_parallel(per_date, run_date, [run_date.isoformat()])

            # combine.py closes out the date: it writes the HRMS upload file
            # and invokes hrms.py itself. Unconditional after a scrape, as in
            # the Streamlit version - combine.py decides for itself whether
            # there is anything worth uploading.
            combine_step = next(
                (w for w in workflow if w.script_name == WORKFLOW_SCRIPT), None)
            if combine_step is None and include_workflow and scrapers:
                combine_step = _workflow_step()
                if combine_step is None:
                    logger.warning(
                        "no combine.py roster entry, so nothing will reach "
                        "HRMS. Run: python manage.py seed_workflow")
            if combine_step is not None:
                # Without this, combine.py rebuilds the upload file from every
                # process that has data for the date and re-sends all of it to
                # HRMS - so running one process rewrote the other processes'
                # daily logs and re-uploaded their agents. COMBINE_PROCESSES
                # pins it to the selection, which is what the Run button means.
                # The nightly run_scrapers.py path is untouched: it selects
                # every scraper, so combine still sees them all.
                scoped = {"COMBINE_PROCESSES": ",".join(p.name for p in scrapers)} \
                    if scrapers else None
                _run_one_safely(combine_step, run_date, [run_date.isoformat()],
                                extra_env=scoped)

            # Any other workflow step picked by hand - an hrms.py re-run, for
            # instance - runs only when combine did not, because combine.py
            # invokes hrms.py itself. Running it again here would upload the
            # same file a second time, which "Select All" would do on every
            # single run since hrms is selected along with everything else.
            if combine_step is None:
                for step in workflow:
                    if step.script_name != WORKFLOW_SCRIPT:
                        _run_one_safely(step, run_date, [run_date.isoformat()])
    finally:
        with _lock:
            _active.difference_update(names)
        close_old_connections()


def start(processes, dates, include_workflow=True):
    """Launch the selected processes, then the workflow step, for each date.

    Returns the names actually started; a process already running is skipped
    rather than launched twice.
    """
    scrapers, workflow, names = [], [], []
    with _lock:
        for process in processes:
            if process.name in _active:
                continue
            _active.add(process.name)
            names.append(process.name)
            if process.kind == ProcessKind.WORKFLOW:
                workflow.append(process)
            else:
                scrapers.append(process)

    if not names:
        return []

    threading.Thread(target=_orchestrate,
                     args=(scrapers, workflow, list(dates), names, include_workflow),
                     daemon=True).start()
    return names


def dates_from_request(mode, single=None, start_date=None, end_date=None):
    """Resolve the three date modes the Streamlit sidebar offered."""
    yesterday = timezone.localdate() - timedelta(days=1)
    if mode == "single":
        return [date_cls.fromisoformat(single)] if single else [yesterday]
    if mode == "range":
        if not (start_date and end_date):
            return []
        first = date_cls.fromisoformat(start_date)
        last = date_cls.fromisoformat(end_date)
        if first > last:
            return []
        span = []
        while first <= last:
            span.append(first)
            first += timedelta(days=1)
        return span
    return [yesterday]
