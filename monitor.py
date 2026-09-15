"""Reports scraper progress to the Django dashboard.

Public API (requirement 12):

    monitor.start("Imagine")
    monitor.log("Imagine", "Login successful")
    monitor.heartbeat("Imagine")
    monitor.success("Imagine", records=1245)
    monitor.failed("Imagine", "Login failed")

Three rules this module holds to, because the scraper must stay independent:

1. **It never raises.** Every call swallows its errors. A dashboard that is
   down, unreachable or misconfigured must not fail a scrape that would
   otherwise have worked.
2. **It never blocks.** Calls drop an event on a bounded queue and return;
   a single daemon thread does the HTTP. If the dashboard hangs, the scraper
   does not. A full queue drops the oldest events rather than growing.
3. **It is optional.** With no MONITOR_API_URL/TOKEN configured it disables
   itself silently, so the scrapers run exactly as they do today.

Only the standard library is used, so the scraper gains no new dependency.
"""

import atexit
import json
import os
import queue
import threading
import urllib.error
import urllib.request
from datetime import date as _date
from datetime import datetime, timedelta

__all__ = ["start", "log", "heartbeat", "success", "warning", "failed", "stop",
           "is_enabled", "configure"]

_QUEUE_MAX = 500
_TIMEOUT = 5.0

_queue = None
_worker = None
_heartbeats = {}          # process -> threading.Event used to stop its beat
_lock = threading.Lock()
_debug = os.getenv("MONITOR_DEBUG", "").lower() in {"1", "true", "yes"}


def _settings():
    """Read config fresh so .env edits do not need a restart of long jobs."""
    base = os.getenv("MONITOR_API_URL", "").rstrip("/")
    token = os.getenv("MONITOR_API_TOKEN", "")
    return base, token


def is_enabled():
    base, token = _settings()
    return bool(base and token)


def configure(url=None, token=None):
    """Point the module at a dashboard explicitly (mainly for tests)."""
    if url is not None:
        os.environ["MONITOR_API_URL"] = url
    if token is not None:
        os.environ["MONITOR_API_TOKEN"] = token


def _note(message):
    if _debug:
        print(f"[monitor] {message}", flush=True)


# ---------------------------------------------------------------------------
# Background sender
# ---------------------------------------------------------------------------

def _post(path, payload):
    base, token = _settings()
    if not (base and token):
        return
    request = urllib.request.Request(
        f"{base}/api/monitor/{path}/",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Monitor-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            if response.status >= 400:
                _note(f"{path} -> HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        _note(f"{path} -> HTTP {exc.code} {exc.reason}")
    except Exception as exc:                      # network down, DNS, timeout
        _note(f"{path} -> {type(exc).__name__}: {exc}")


def _drain():
    while True:
        item = _queue.get()
        if item is None:                          # shutdown sentinel
            _queue.task_done()
            return
        path, payload = item
        try:
            _post(path, payload)
        finally:
            _queue.task_done()


def _ensure_worker():
    global _queue, _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _queue = queue.Queue(maxsize=_QUEUE_MAX)
        _worker = threading.Thread(target=_drain, name="monitor-sender", daemon=True)
        _worker.start()


def _send(path, payload):
    """Queue an event. Never raises, never blocks."""
    if not is_enabled():
        return
    try:
        _ensure_worker()
        try:
            _queue.put_nowait((path, payload))
        except queue.Full:
            # Prefer the newest events: discard one old item and retry once.
            try:
                _queue.get_nowait()
                _queue.task_done()
                _queue.put_nowait((path, payload))
            except Exception:
                _note("queue full, event dropped")
    except Exception as exc:
        _note(f"send failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

def _default_date():
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _base_payload(process, run_date=None, run_id=None, script_path=None):
    if isinstance(run_date, (_date, datetime)):
        run_date = run_date.strftime("%Y-%m-%d")
    payload = {"process": process, "run_date": run_date or _default_date()}
    if run_id:
        payload["run_id"] = run_id
    if script_path:
        # Lets the dashboard resolve which roster entry this is, so
        # Imagine / Imagine Clean / Imagine_Disposition stay distinct even
        # though all three run from the same folder.
        payload["script_path"] = script_path
    return payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start(process, run_date=None, run_id=None, script_path=None, heartbeat_seconds=None):
    """Mark a run as RUNNING and begin sending heartbeats."""
    _send("start", _base_payload(process, run_date, run_id, script_path))
    interval = heartbeat_seconds or int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "30"))
    _start_heartbeat(process, run_date, run_id, script_path, interval)


def log(process, message, stage="", level="INFO", run_date=None, run_id=None,
        script_path=None):
    payload = _base_payload(process, run_date, run_id, script_path)
    payload.update({"message": str(message)[:4000], "stage": stage, "level": level})
    _send("log", payload)


def heartbeat(process, run_date=None, run_id=None, script_path=None):
    _send("heartbeat", _base_payload(process, run_date, run_id, script_path))


def _finish(endpoint, process, run_date, run_id, script_path, **extra):
    _stop_heartbeat(process)
    payload = _base_payload(process, run_date, run_id, script_path)
    payload.update({k: v for k, v in extra.items() if v is not None})
    _send(endpoint, payload)
    flush(timeout=5)


def success(process, records=None, uploaded=None, failed_records=None,
            run_date=None, run_id=None, script_path=None):
    _finish("success", process, run_date, run_id, script_path,
            records_scraped=records, records_uploaded=uploaded,
            records_failed=failed_records)


def warning(process, message=None, records=None, run_date=None, run_id=None,
            script_path=None):
    _finish("warning", process, run_date, run_id, script_path,
            records_scraped=records, error=message)


def failed(process, error="", run_date=None, run_id=None, script_path=None,
           evidence_path=None):
    _finish("failed", process, run_date, run_id, script_path,
            error=str(error)[:5000], evidence_path=evidence_path)


# ---------------------------------------------------------------------------
# Heartbeat thread (requirement 11)
# ---------------------------------------------------------------------------

def _start_heartbeat(process, run_date, run_id, script_path, interval):
    if not is_enabled():
        return
    _stop_heartbeat(process)
    stop_event = threading.Event()

    def beat():
        # Event.wait doubles as the sleep and the stop signal, so shutdown is
        # immediate rather than up to `interval` seconds late.
        while not stop_event.wait(interval):
            heartbeat(process, run_date, run_id, script_path)

    thread = threading.Thread(target=beat, name=f"monitor-hb-{process}", daemon=True)
    with _lock:
        _heartbeats[process] = stop_event
    thread.start()


def _stop_heartbeat(process):
    with _lock:
        event = _heartbeats.pop(process, None)
    if event is not None:
        event.set()


def flush(timeout=10):
    """Wait for queued events to be sent. Best effort."""
    if _queue is None:
        return
    deadline = threading.Event()
    waiter = threading.Thread(target=lambda: (_queue.join(), deadline.set()), daemon=True)
    waiter.start()
    deadline.wait(timeout)


def stop():
    """Stop all heartbeats and drain the queue. Called automatically at exit."""
    with _lock:
        events = list(_heartbeats.values())
        _heartbeats.clear()
    for event in events:
        event.set()
    flush(timeout=5)


atexit.register(stop)
