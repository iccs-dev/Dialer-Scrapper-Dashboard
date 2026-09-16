"""JSON APIs for the monitoring dashboard.

Two audiences, two authentication schemes (requirement 16):

* scrapers  -> POST /api/monitor/...  authenticated by a shared token in the
               `X-Monitor-Token` header. Scrapers are headless and have no
               session, so a token is the right fit. The token lives in .env,
               never in the frontend.
* dashboard -> GET  /api/dashboard/... authenticated by the normal Django
               session (@login_required). The browser already has a session
               after login, so no token is exposed to JavaScript.

Plain Django JsonResponse is used rather than DRF - the payloads are simple
and one less dependency is one less thing to maintain.
"""

import json
import logging
from datetime import date as date_cls
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from . import media_reader
from .consumers import DASHBOARD_GROUP
from .models import Process, ProcessKind, ProcessLog, ProcessRun, Status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def monitor_token_required(view):
    """Shared-token auth for scraper callbacks.

    CSRF is exempt because the caller is a script, not a browser form - it has
    no cookie and no session to forge. The token is what authenticates it, so
    a missing or blank configured token refuses everything rather than
    silently accepting all callers.
    """

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        expected = settings.MONITOR_API_TOKEN
        if not expected:
            return JsonResponse(
                {"error": "MONITOR_API_TOKEN is not configured on the server"},
                status=503,
            )
        supplied = request.headers.get("X-Monitor-Token", "")
        if supplied != expected:
            return JsonResponse({"error": "invalid or missing token"}, status=401)
        return view(request, *args, **kwargs)

    return wrapper


def _payload(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None


def _parse_date(value, default=None):
    if not value:
        return default or (timezone.localdate() - timedelta(days=1))
    if isinstance(value, date_cls):
        return value
    return date_cls.fromisoformat(str(value))


def _resolve_process(data):
    """Which roster entry is calling.

    script_path is preferred over the bare name: three roster entries can
    share one folder (Imagine / Imagine Clean / Imagine_Disposition), and only
    the script tells them apart. The name is the fallback for callers that do
    not know their own path.
    """
    script_path = (data.get("script_path") or "").strip().replace("\\", "/")
    if script_path:
        # A path with no directory means the project root, which is where
        # combine.py and hrms.py live. Splitting on "/" unconditionally and
        # requiring two parts silently skipped them, so every callback they
        # made fell through to the name lookup and then 404ed.
        working_dir, _, script_name = script_path.rpartition("/")
        match = Process.objects.filter(
            working_dir=working_dir, script_name=script_name
        ).first()
        if match is not None:
            return match

    name = (data.get("process") or "").strip()
    return Process.objects.filter(name=name).first() if name else None


def _get_run(data, create=False):
    """Find (or create) the run a scraper callback refers to.

    Matching is by run_id when given - that is the id core/runlog.py already
    generates - otherwise by the newest run for that process and date.
    """
    name = (data.get("process") or "").strip()
    if not name and not data.get("script_path"):
        return None, JsonResponse({"error": "process is required"}, status=400)

    process = _resolve_process(data)
    if process is None:
        return None, JsonResponse(
            {"error": f"unknown process {name or data.get('script_path')!r}"},
            status=404,
        )

    run_date = _parse_date(data.get("run_date"))
    run_id = (data.get("run_id") or "").strip()

    query = ProcessRun.objects.filter(process=process, run_date=run_date)
    run = query.filter(run_id=run_id).first() if run_id else query.first()
    if run is None and create:
        run = ProcessRun.objects.create(
            process=process, run_date=run_date, run_id=run_id
        )
    if run is None:
        return None, JsonResponse(
            {"error": f"no run for {name} on {run_date}; call start first"},
            status=404,
        )
    return run, None


def _broadcast(run, reason="update"):
    """Tell every connected browser that something changed.

    Only a signal is sent, not the state itself - the browser re-fetches from
    the REST API, so the socket can never disagree with it. See
    monitoring/consumers.py for the reasoning.

    A broken channel layer must never break a scraper callback, so failures
    here are logged and swallowed rather than raised.
    """
    try:
        layer = get_channel_layer()
        if layer is None:
            return None
        async_to_sync(layer.group_send)(DASHBOARD_GROUP, {
            "type": "dashboard.refresh",
            "reason": reason,
            "process": run.process.name if run is not None else None,
            "status": run.status if run is not None else None,
        })
    except Exception:
        logger.warning("dashboard broadcast failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Scraper-facing endpoints  (monitor.start / log / heartbeat / success / failed)
# ---------------------------------------------------------------------------

@monitor_token_required
@require_POST
def monitor_start(request):
    data = _payload(request)
    if data is None:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    run, error = _get_run(data, create=True)
    if error:
        return error

    with transaction.atomic():
        run.status = Status.RUNNING
        run.started_at = timezone.now()
        run.last_heartbeat = timezone.now()
        run.completed_at = None
        run.error = ""
        if data.get("run_id"):
            run.run_id = data["run_id"]
        run.save()
    _broadcast(run)
    return JsonResponse({"ok": True, "run": run.id, "status": run.status})


@monitor_token_required
@require_POST
def monitor_log(request):
    data = _payload(request)
    if data is None:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    message = (data.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)
    run, error = _get_run(data, create=True)
    if error:
        return error

    entry = ProcessLog.objects.create(
        run=run,
        stage=(data.get("stage") or "").strip(),
        level=(data.get("level") or "INFO").upper(),
        message=message,
    )
    # A log line is also proof of life, so it counts as a heartbeat.
    ProcessRun.objects.filter(pk=run.pk).update(last_heartbeat=timezone.now())
    _broadcast(run)
    return JsonResponse({"ok": True, "log": entry.id})


@monitor_token_required
@require_POST
def monitor_heartbeat(request):
    data = _payload(request)
    if data is None:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    run, error = _get_run(data)
    if error:
        return error

    updates = {"last_heartbeat": timezone.now()}
    # A heartbeat from a run we had written off revives it.
    if run.status == Status.NOT_RESPONDING:
        updates["status"] = Status.RUNNING
    ProcessRun.objects.filter(pk=run.pk).update(**updates)
    _broadcast(run)
    return JsonResponse({"ok": True})


def _finish(request, status):
    data = _payload(request)
    if data is None:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    run, error = _get_run(data, create=True)
    if error:
        return error

    with transaction.atomic():
        run.status = status
        run.completed_at = timezone.now()
        if run.started_at is None:
            run.started_at = run.completed_at
        for field in ("records_scraped", "records_uploaded", "records_failed"):
            if data.get(field) is not None:
                setattr(run, field, int(data[field]))
        if data.get("error"):
            run.error = str(data["error"])[:5000]
        if data.get("evidence_path"):
            run.evidence_path = str(data["evidence_path"])[:255]
        run.save()
    _broadcast(run)
    return JsonResponse(
        {"ok": True, "run": run.id, "status": run.status,
         "duration_seconds": run.duration_seconds}
    )


@monitor_token_required
@require_POST
def monitor_success(request):
    return _finish(request, Status.SUCCESS)


@monitor_token_required
@require_POST
def monitor_warning(request):
    return _finish(request, Status.WARNING)


@monitor_token_required
@require_POST
def monitor_failed(request):
    return _finish(request, Status.FAILED)


# ---------------------------------------------------------------------------
# Heartbeat sweep  (requirement 11)
# ---------------------------------------------------------------------------

def sweep_stale_runs():
    """Flip RUNNING runs with no recent heartbeat to NOT_RESPONDING.

    Called before serving dashboard state, so the screen can never show a
    crashed scraper as Running - even if no scheduled sweep has run yet.
    Returns the number of runs changed.
    """
    timeout = settings.HEARTBEAT_TIMEOUT_SECONDS
    changed = 0
    for run in ProcessRun.objects.filter(status=Status.RUNNING):
        if run.is_stale(timeout) and run.mark_not_responding():
            changed += 1
            _broadcast(run)
    return changed


# ---------------------------------------------------------------------------
# Dashboard-facing endpoints
# ---------------------------------------------------------------------------

def _run_json(run):
    return {
        "id": run.id,
        "process": run.process.name,
        "category": run.process.category,
        "run_date": run.run_date.isoformat(),
        "run_id": run.run_id,
        "status": run.status,
        "status_label": run.get_status_display(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration": run.duration_display,
        "records_scraped": run.records_scraped,
        "records_uploaded": run.records_uploaded,
        "records_failed": run.records_failed,
        "error": run.error,
        "evidence_path": run.evidence_path,
        "last_heartbeat": run.last_heartbeat.isoformat() if run.last_heartbeat else None,
    }


@login_required
@require_GET
def dashboard_state(request):
    """Current status of every process for one date - the live tiles.

    Mirrors the Streamlit Scraper tab: per-process status plus the
    Success / Failed / Warning / Pending counters.
    """
    sweep_stale_runs()
    run_date = _parse_date(request.GET.get("date"))

    # combine and hrms report runs like anything else, but they are workflow
    # steps rather than scrapers and are not shown as tiles of their own.
    processes = Process.objects.exclude(kind=ProcessKind.WORKFLOW)
    latest = {}
    for run in ProcessRun.objects.filter(run_date=run_date).select_related("process"):
        # Queryset ordering puts the newest first, so keep the first seen.
        latest.setdefault(run.process.name, run)

    rows, summary = [], {s.value: 0 for s in Status}
    for process in processes:
        run = latest.get(process.name)
        status = run.status if run else Status.PENDING
        summary[status] = summary.get(status, 0) + 1
        rows.append({
            "process": process.name,
            "category": process.category,
            "is_active": process.is_active,
            "schedule_type": process.schedule_type,
            "status": status,
            "run": _run_json(run) if run else None,
        })

    # Whether this date's data actually reached HRMS. A run finishing is not
    # the same thing: the scrape can succeed, combine can build the upload
    # file, and the upload can still never have happened - so the banner must
    # not call the day a success on the strength of exit codes alone.
    already = media_reader.already_processed(run_date)
    hrms_pushed = False
    hrms_detail = {}
    for process in processes:
        statuses, _ = media_reader.daily_log_statuses(process.name, run_date)
        pushed = (statuses.get("HRMS") == "SUCCESS"
                  or already.get(process.name, 0) > 0)
        if latest.get(process.name):
            hrms_detail[process.name] = statuses.get("HRMS", "NOT RUN")
        hrms_pushed = hrms_pushed or pushed

    return JsonResponse({
        "date": run_date.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "heartbeat_timeout_seconds": settings.HEARTBEAT_TIMEOUT_SECONDS,
        "processes": rows,
        "summary": summary,
        "hrms_pushed": hrms_pushed,
        "hrms_detail": hrms_detail,
    })


@login_required
@require_GET
def dashboard_logs(request):
    """Recent log lines - the Live logs panel.

    Filtered by date, and optionally by process. An empty `process` keeps the
    previous behaviour of showing every process for the date.
    """
    run_id = request.GET.get("run")
    process = (request.GET.get("process") or "").strip()
    limit = min(int(request.GET.get("limit", 300)), 2000)

    logs = ProcessLog.objects.select_related("run__process")
    if run_id:
        logs = logs.filter(run_id=run_id)
    else:
        run_date = _parse_date(request.GET.get("date"))
        logs = logs.filter(run__run_date=run_date)
    if process:
        # Exact name, not a contains match: "DMI" must never pull in
        # "DMI Clean_b" or "DMI_Disposition".
        logs = logs.filter(run__process__name=process)

    entries = list(logs.order_by("-timestamp", "-id")[:limit])
    entries.reverse()
    return JsonResponse({
        "count": len(entries),
        "process": process,
        "date": request.GET.get("date") or "",
        "logs": [{
            "id": e.id,
            "process": e.run.process.name,
            "timestamp": e.timestamp.isoformat(),
            "stage": e.stage,
            "level": e.level,
            "message": e.message,
        } for e in entries],
    })


@login_required
@require_GET
def dashboard_matrix(request):
    """The Monitoring tab matrix, read from the scrapers' own output.

    Cell rule:
      green  N   scraped and confirmed pushed to HRMS
      yellow N*  scraped (and possibly combined) but not in HRMS
      red    ""  no data

    "Pushed" is taken from the HRMS line of the process's own daily log, which
    hrms.py writes after HRMS answers. The combined upload file is deliberately
    not the signal: that file existing only proves combine.py built it, and a
    date can sit there fully combined with the upload never having happened -
    which is exactly the state the star is there to make visible.
    """
    end = _parse_date(request.GET.get("end"))
    start = _parse_date(request.GET.get("start"), end - timedelta(days=6))
    if start > end:
        return JsonResponse({"error": "start must be on or before end"}, status=400)
    if (end - start).days > 92:
        return JsonResponse({"error": "range is limited to 92 days"}, status=400)

    processes = list(Process.objects.filter(is_active=True,
                                            kind=ProcessKind.SCRAPER))
    # `process` is a comma-separated list; empty means every process, which is
    # the previous behaviour.
    selected = [n.strip() for n in (request.GET.get("process") or "").split(",")
                if n.strip()]
    if selected:
        chosen = set(selected)
        processes = [p for p in processes if p.name in chosen]
    names = [p.name for p in processes]

    rows = []
    totals = {"green": 0, "yellow": 0, "red": 0, "running": 0}
    current = end
    while current >= start:
        uploads, upload_total = media_reader.upload_info(current)
        uploads = uploads or {}
        already = media_reader.already_processed(current)

        cells = {}
        hrms_done = False
        for process in processes:
            scraped = media_reader.scrape_row_count(process, current)
            uploaded = uploads.get(process.name, 0)
            statuses, _ = media_reader.daily_log_statuses(process.name, current)
            # "Already processed" means HRMS refused the rows because the day
            # was closed - the data is in HRMS, so it counts as done.
            done = (statuses.get("HRMS") == "SUCCESS"
                    or already.get(process.name, 0) > 0)
            hrms_done = hrms_done or done

            # combine.py builds one upload file for every process that has
            # data, so running any process re-runs combine and HRMS for all of
            # them. While that is in flight the other processes' stages read
            # RUNNING, and calling that "never reached HRMS" turns a finished
            # green day yellow until the re-run lands. In progress is not the
            # same as absent, so it gets its own state.
            in_progress = "RUNNING" in (statuses.get("HRMS"),
                                        statuses.get("COMBINE"))

            if scraped > 0 and done:
                state, totals["green"] = "green", totals["green"] + 1
                text = str(scraped)
            elif scraped > 0 and in_progress:
                state, totals["running"] = "running", totals["running"] + 1
                text = str(scraped)
            elif scraped > 0:
                state, totals["yellow"] = "yellow", totals["yellow"] + 1
                text = f"{scraped}*"
            else:
                state, totals["red"] = "red", totals["red"] + 1
                text = ""
            cells[process.name] = {
                "state": state, "text": text, "scraped": scraped,
                "uploaded": uploaded,
                "hrms": statuses.get("HRMS", "NOT RUN"),
                "combine": statuses.get("COMBINE", "NOT RUN"),
            }

        # With one process selected, the upload columns have to describe that
        # process too - showing the whole file's row count beside a single
        # process's cells would read as if those rows were all its own.
        if selected:
            upload_rows = sum(uploads.get(n, 0) for n in selected)
            in_upload = sum(1 for n in selected if uploads.get(n))
        else:
            upload_rows, in_upload = upload_total, len(uploads)

        detail = media_reader.hrms_process_detail(current)
        if detail and selected:
            detail = {k: v for k, v in detail.items() if k in set(selected)}

        rows.append({
            "date": current.isoformat(),
            "cells": cells,
            "upload_rows": upload_rows,
            "processes_in_upload": in_upload,
            "processes_scraped": sum(1 for c in cells.values() if c["scraped"] > 0),
            "hrms": hrms_done,
            "hrms_detail": detail,
        })
        current -= timedelta(days=1)

    return JsonResponse({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "processes": names,
        "selected_processes": selected,
        "rows": rows,
        "totals": totals,
    })



@login_required
@require_GET
def dashboard_dataset(request):
    """Status of a secondary dataset - APR Clean or Disposition.

    Same shape as the matrix so the front end can reuse its rendering, but
    counted over a different folder. Nothing is invented: a date/process with
    no file on disk is reported as no data, which is the honest answer while
    these datasets are still being filled in.
    """
    kind = (request.GET.get("kind") or "").strip()
    folder = media_reader.DATASET_FOLDERS.get(kind)
    log_folder = media_reader.DATASET_LOG_FOLDERS.get(kind)
    if not folder:
        return JsonResponse(
            {"error": f"unknown dataset {kind!r}; expected one of "
                      f"{sorted(media_reader.DATASET_FOLDERS)}"}, status=400)

    end = _parse_date(request.GET.get("end"))
    start = _parse_date(request.GET.get("start"), end - timedelta(days=6))
    if start > end:
        return JsonResponse({"error": "start must be on or before end"}, status=400)
    if (end - start).days > 92:
        return JsonResponse({"error": "range is limited to 92 days"}, status=400)

    # Both datasets are produced by their own processes, so each tab reports on
    # those by name - "GOQII Clean", "DMI Disposition" - the way the scraper
    # tabs report on scrapers. Each roster row already says where its output
    # lands and what the file is called, so the count comes from that rather
    # than from a folder guessed from the process name. Listing the scrapers
    # instead would give a permanently red row to every process that has no
    # such report at all, which reads as breakage rather than as absence.
    kinds = {"apr_clean": ProcessKind.CLEANER,
             "disposition": ProcessKind.DISPOSITION}
    processes = list(Process.objects.filter(
        is_active=True, kind=kinds[kind]))
    selected = [n.strip() for n in (request.GET.get("process") or "").split(",")
                if n.strip()]
    if selected:
        chosen = set(selected)
        processes = [p for p in processes if p.name in chosen]
    names = [p.name for p in processes]

    # Disposition is its own workflow and never reaches HRMS.
    uploads_to_hrms = kind != "disposition"

    rows = []
    totals = {"pushed": 0, "not_pushed": 0, "no_data": 0}
    current = end
    while current >= start:
        already = media_reader.already_processed(current)
        cells = {}
        for process in processes:
            # Cleaned APR workbooks are written headerless; counting them the
            # default way loses the first agent every time. Disposition exports
            # carry their header, so the same treatment would drop a row.
            count = media_reader.scrape_row_count(
                process, current, headerless=(kind == "apr_clean"))
            # The row's standing belongs to the source process, since that is
            # the workflow its file belongs to.
            log_process = (process.output_dir.split("/")[0]
                           if process.output_dir else process.name)
            statuses, _ = media_reader.daily_log_statuses(
                log_process, current, folder=log_folder)
            if uploads_to_hrms:
                complete = (statuses.get("HRMS") == "SUCCESS"
                            or already.get(process.name, 0) > 0)
            else:
                # Disposition has no HRMS stage, so there is no second step to
                # be waiting on: the report existing is the whole job. Starring
                # every cell would report a gap that does not exist.
                complete = True
            if count > 0 and complete:
                state, key, text = "green", "pushed", str(count)
            elif count > 0:
                state, key, text = "yellow", "not_pushed", f"{count}*"
            else:
                state, key, text = "red", "no_data", ""
            totals[key] += 1
            cells[process.name] = {"state": state, "text": text, "rows": count,
                                   "hrms": statuses.get("HRMS", "NOT RUN")}
        rows.append({"date": current.isoformat(), "cells": cells})
        current -= timedelta(days=1)

    return JsonResponse({
        "kind": kind,
        "folder": folder,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "processes": names,
        "rows": rows,
        "totals": totals,
        "has_data": totals["pushed"] + totals["not_pushed"] > 0,
        # Disposition has no HRMS stage; saying so lets the UI explain a
        # "Pushed to HRMS" card that is legitimately always zero.
        "uploads_to_hrms": uploads_to_hrms,
    })
