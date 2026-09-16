"""Dashboard pages."""

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Process, ProcessKind
from .runner import active_processes, dates_from_request, start


@login_required
def dashboard(request):
    """The single page at /dashboard/.

    Server-renders only what does not change (the process roster grouped by
    category, exactly like the Streamlit sidebar's expanders). Everything live
    - statuses, logs, the matrix - is fetched by JavaScript from the APIs, so
    Stage 5 can swap polling for a WebSocket without touching this template.
    """
    # Workflow steps (combine.py, hrms.py) are registered so their monitoring
    # callbacks resolve and their logs reach the Logs panel, but they are not
    # processes anyone selects and scrapes - combine runs automatically after
    # the scrapers - so they are kept out of the roster and the tiles.
    processes = Process.objects.exclude(kind=ProcessKind.WORKFLOW)
    categories = {}
    for process in processes:
        categories.setdefault(process.category or "Other", []).append(process)

    # The two dataset tabs report on their own producers, so their Process
    # filters list those rather than the whole roster. Offering every process
    # there let someone tick an APR scraper in the Disposition filter and get
    # an empty table, which reads as missing data rather than a wrong filter.
    def by_kind(kind):
        return [p for p in processes if p.kind == kind]

    yesterday = timezone.localdate() - timedelta(days=1)
    return render(request, "monitoring/dashboard.html", {
        "categories": sorted(categories.items()),
        "cleaners": by_kind(ProcessKind.CLEANER),
        "dispositions": by_kind(ProcessKind.DISPOSITION),
        "process_count": processes.count(),
        "active_count": processes.filter(is_active=True).count(),
        "yesterday": yesterday.isoformat(),
        "week_ago": (yesterday - timedelta(days=6)).isoformat(),
        "heartbeat_timeout": request.GET.get("hb", ""),
    })


@login_required
@require_POST
def run_scraper(request):
    """Launch the selected processes for the selected dates."""
    names = request.POST.getlist("processes")
    if not names:
        return JsonResponse({"error": "Select at least one process"}, status=400)

    dates = dates_from_request(
        request.POST.get("date_mode", "yesterday"),
        request.POST.get("single_date"),
        request.POST.get("start_date"),
        request.POST.get("end_date"),
    )
    if not dates:
        return JsonResponse({"error": "Invalid date selection"}, status=400)

    selected = list(Process.objects.filter(name__in=names, is_active=True))
    skipped = sorted(set(names) - {p.name for p in selected})
    if not selected:
        return JsonResponse(
            {"error": "None of the selected processes are available on this machine",
             "skipped": skipped}, status=400)

    started = start(selected, dates)
    return JsonResponse({
        "ok": True,
        "started": started,
        "busy": sorted(set(p.name for p in selected) - set(started)),
        "skipped": skipped,
        "dates": [d.isoformat() for d in dates],
    })


@login_required
def run_status(request):
    """Which processes the dashboard currently has running."""
    return JsonResponse({"active": active_processes()})
