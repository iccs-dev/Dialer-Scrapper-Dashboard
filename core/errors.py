"""Central error handling: classify, recover, record evidence, alert.

    Playwright action -> retry/recovery -> still failed?
        -> central handler -> log + screenshot + trace -> history -> email

Classification is done on the exception's type name and message rather than by
importing Playwright, so this module stays importable (by the monitor, for
instance) on a machine without Playwright installed.
"""

import html
import json
import platform
import re
import smtplib
import socket
import traceback
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from core.config import settings
from core.runlog import Stage


# ---------------------------------------------------------------------------
# Recovery vocabulary
# ---------------------------------------------------------------------------

class Action:
    RETRY = "retry"                    # transient: try the same step again
    RELOCATE = "relocate"              # element went stale: re-resolve, retry
    RELOGIN = "relogin"                # session expired: log in again, retry
    RESTART_BROWSER = "restart_browser"  # browser/page died: relaunch, retry
    ALERT = "alert"                    # cannot self-heal: evidence + email
    FATAL = "fatal"                    # never retry (bad credentials)


class Category:
    TIMEOUT = "timeout"
    NETWORK = "network"
    STALE_ELEMENT = "stale_element"
    SESSION_EXPIRED = "session_expired"
    BROWSER_FAILURE = "browser_failure"
    UI_CHANGE = "ui_change"
    INVALID_CREDENTIALS = "invalid_credentials"
    VALIDATION = "validation"
    UNKNOWN = "unknown"


#: What to do for each category, and whether the attempt may be repeated.
POLICY = {
    Category.TIMEOUT: (Action.RETRY, True),
    Category.NETWORK: (Action.RETRY, True),
    Category.STALE_ELEMENT: (Action.RELOCATE, True),
    Category.SESSION_EXPIRED: (Action.RELOGIN, True),
    Category.BROWSER_FAILURE: (Action.RESTART_BROWSER, True),
    Category.UI_CHANGE: (Action.ALERT, False),
    Category.INVALID_CREDENTIALS: (Action.FATAL, False),
    Category.VALIDATION: (Action.ALERT, False),
    Category.UNKNOWN: (Action.RETRY, True),
}

#: How a stage is named in an [ERROR] line.
STAGE_LABEL = {
    "startup": "Startup",
    "browser_start": "Browser start",
    "login": "Login",
    "navigation": "Navigation",
    "date_selection": "Date selection",
    "scraping": "Scraping",
    "download": "Download",
    "validation": "Validation",
    "processing": "Processing",
    "hrms_upload": "Upload",
    "completed": "Run",
}


def _one_line(text, limit=200):
    """Playwright messages carry a multi-line 'Call log:' block."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


HUMAN_CAUSE = {
    Category.TIMEOUT: "The page did not respond in time",
    Category.NETWORK: "The dialer server could not be reached",
    Category.STALE_ELEMENT: "The page changed while it was being used",
    Category.SESSION_EXPIRED: "The dialer session expired",
    Category.BROWSER_FAILURE: "The browser stopped unexpectedly",
    Category.UI_CHANGE: "A required element was not found - the dialer UI has probably changed",
    Category.INVALID_CREDENTIALS: "The dialer rejected the credentials",
    Category.VALIDATION: "The downloaded report failed validation",
    Category.UNKNOWN: "Unexpected error",
}


# ---------------------------------------------------------------------------
# Exceptions raised by our own code
# ---------------------------------------------------------------------------

class ScraperError(Exception):
    """Base class carrying an explicit category."""

    category = Category.UNKNOWN


class UiChangedError(ScraperError):
    """No fallback locator matched - the page markup no longer fits."""

    category = Category.UI_CHANGE

    def __init__(self, description, tried):
        self.description = description
        self.tried = list(tried)
        super().__init__(
            f"Could not locate {description}; tried {len(self.tried)} strategies: "
            + ", ".join(self.tried)
        )


class InvalidCredentialsError(ScraperError):
    category = Category.INVALID_CREDENTIALS


class SessionExpiredError(ScraperError):
    category = Category.SESSION_EXPIRED


class DownloadValidationError(ScraperError):
    category = Category.VALIDATION


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_NETWORK_MARKERS = (
    "net::err", "econnrefused", "econnreset", "ehostunreach", "enetunreach",
    "connection refused", "connection reset", "name_not_resolved",
    "address_unreachable", "connection_timed_out", "empty_response",
    "temporary failure in name resolution",
)
_BROWSER_MARKERS = (
    "target closed", "target page, context or browser has been closed",
    "browser has been closed", "browser closed", "crashed",
    "connection closed", "playwright was disconnected", "browserType.launch",
)
_STALE_MARKERS = (
    "element is not attached", "detached from the dom", "stale element",
    "element handle is disposed", "node is detached",
)
_SESSION_MARKERS = (
    "session expired", "session timed out", "please login", "login required",
    "not authenticated", "sign in to continue",
)
_CREDENTIAL_MARKERS = (
    "invalid password", "incorrect password", "invalid credential",
    "invalid username", "authentication failed", "login failed",
    "unauthorized", "please enter valid password", "wrong password",
)
_UI_MARKERS = (
    "strict mode violation", "resolved to 0 elements",
    "waiting for locator", "no element matching",
)


def classify(exc):
    """Return the Category that best describes *exc*."""
    if isinstance(exc, ScraperError):
        return exc.category

    name = type(exc).__name__.lower()
    text = f"{type(exc).__name__}: {exc}".lower()

    if any(marker in text for marker in _CREDENTIAL_MARKERS):
        return Category.INVALID_CREDENTIALS
    if any(marker in text for marker in _SESSION_MARKERS):
        return Category.SESSION_EXPIRED
    if any(marker in text for marker in _BROWSER_MARKERS):
        return Category.BROWSER_FAILURE
    if any(marker in text for marker in _STALE_MARKERS):
        return Category.STALE_ELEMENT
    if any(marker in text for marker in _NETWORK_MARKERS):
        return Category.NETWORK
    if "timeout" in name or "timeouterror" in name:
        # A Playwright timeout on a locator is usually a changed UI; on a
        # navigation it is usually slowness. The message tells them apart.
        if any(marker in text for marker in _UI_MARKERS):
            return Category.UI_CHANGE
        return Category.TIMEOUT
    if any(marker in text for marker in _UI_MARKERS):
        return Category.UI_CHANGE
    if isinstance(exc, (ConnectionError, socket.timeout, OSError)):
        return Category.NETWORK
    return Category.UNKNOWN


def decide(exc):
    """(category, action, retryable) for *exc*."""
    category = classify(exc)
    action, retryable = POLICY.get(category, (Action.RETRY, True))
    return category, action, retryable


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def _origin(exc):
    """Where the failure came from: file, function, line.

    Prefers the deepest frame inside this project. The raw last frame is
    usually somewhere in Playwright's internals, which tells a maintainer
    nothing about which step broke.
    """
    empty = {"file": None, "function": None, "line": None}
    tb = exc.__traceback__
    if tb is None:
        return empty
    frames = traceback.extract_tb(tb)
    if not frames:
        return empty

    import common

    root = str(Path(common.CODE_ROOT).resolve())
    ours = [
        f for f in frames
        if str(Path(f.filename).resolve()).startswith(root)
        and "site-packages" not in f.filename
    ]
    chosen = ours[-1] if ours else frames[-1]
    return {
        "file": chosen.filename,
        "function": chosen.name,
        "line": chosen.lineno,
        "code": (chosen.line or "").strip() or None,
        "in_project": bool(ours),
    }


def _page_state(page):
    """URL and title, tolerating a page that is already gone."""
    state = {"url": None, "title": None}
    if page is None:
        return state
    try:
        state["url"] = page.url
    except Exception:
        pass
    try:
        state["title"] = page.title()
    except Exception:
        pass
    return state


def capture_evidence(ctx, exc, page=None, session=None, stage=None, attempts=None):
    """Write screenshot, trace and error.json; append to the error history."""
    stage = stage or ctx.failed_stage or ctx.stage
    category, action, _ = decide(exc)
    evidence_dir = ctx.evidence_dir()

    screenshot_path = None
    if settings.screenshot_on_error and page is not None:
        target = evidence_dir / "screenshot.png"
        # A page that failed to load can hang a full-page capture (it waits on
        # fonts), so use a short timeout and fall back to the viewport only.
        for attempt_kwargs in ({"full_page": True, "timeout": 8000},
                               {"full_page": False, "timeout": 4000}):
            try:
                page.screenshot(path=str(target), **attempt_kwargs)
                screenshot_path = str(target)
                break
            except Exception as shot_exc:
                last_error = shot_exc
        if screenshot_path is None:
            ctx.detail(f"Could not capture screenshot: {last_error}")

    trace_path = None
    if settings.trace_on_error and session is not None:
        try:
            trace_path = session.stop_trace(evidence_dir / "trace.zip")
        except Exception as trace_exc:
            ctx.detail(f"Could not save trace: {trace_exc}")

    state = _page_state(page)
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "run_id": ctx.run_id,
        "process": ctx.process,
        "target_date": ctx.date_key,
        "stage": stage,
        "category": category,
        "action": action,
        "attempts": attempts,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "origin": _origin(exc),
        "url": state["url"],
        "page_title": state["title"],
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        # UiChangedError knows exactly which strategies were attempted; keep
        # them as a list so the alert can show them one per line instead of
        # one long comma-joined string.
        "locators_tried": list(getattr(exc, "tried", []) or []),
        "log_path": str(ctx.log_path),
        "screenshot_path": screenshot_path,
        "trace_path": trace_path,
        "host": platform.node(),
    }

    try:
        (evidence_dir / "error.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
    except Exception as write_exc:
        ctx.warn(f"Could not write error.json: {write_exc}")

    try:
        with ctx.history_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception as write_exc:
        ctx.warn(f"Could not append error history: {write_exc}")

    return record


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

_ROW = "<tr><th align='left' style='padding:4px 10px;background:#f2f2f2;'>{}</th><td style='padding:4px 10px;'>{}</td></tr>"


def render_alert_html(record):
    """The alert body. Credentials never reach this function."""
    def row(label, value):
        return _ROW.format(html.escape(label), html.escape(str(value if value else "-")))

    # Deliberately just these four. Everything else the run captured - run id,
    # category, page URL/title, origin, attempts, host and the paths to the
    # log, screenshot and trace - is still recorded in error.json and named in
    # the [ERROR] lines of the run log; it is only kept out of the email.
    rows = "".join([
        row("Process", record["process"]),
        row("Target Date", record["target_date"]),
        row("Stage", record["stage"]),
        row("Error Message", f"{record['exception_type']}: {record['exception_message']}"),
    ])

    tried = record.get("locators_tried") or []
    tried_block = ""
    if tried:
        items = "".join(f"<li><code>{html.escape(str(t))}</code></li>" for t in tried)
        tried_block = (
            "<h3 style='margin:18px 0 6px;font-size:14px;'>Locators tried "
            f"({len(tried)})</h3><ol style='margin:0 0 8px 18px;'>{items}</ol>"
        )
    exact = html.escape(
        f"{record['exception_type']}: {record['exception_message']}"
    )
    return f"""
<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1a1a1a;">
  <div style="padding:10px 12px;border-left:4px solid #c62828;background:#ffebee;
              color:#b71c1c;font-weight:600;margin-bottom:14px;">
    Scraper failed: {html.escape(record['process'])} - {html.escape(record['target_date'])}
  </div>
  <table style="border-collapse:collapse;border:1px solid #e0e0e0;">{rows}</table>
  {tried_block}
  <h3 style="margin:18px 0 6px;font-size:14px;">Exact error</h3>
  <pre style="background:#fff8e1;border:1px solid #ffe082;padding:10px;
              font-size:12px;white-space:pre-wrap;">{exact}</pre>
  <h3 style="margin:18px 0 6px;font-size:14px;">Traceback</h3>
  <pre style="background:#fafafa;border:1px solid #e0e0e0;padding:10px;
              font-size:11px;overflow-x:auto;">{html.escape(record['traceback'])}</pre>
</body></html>
"""


def send_alert(ctx, record):
    """Email the failure. Returns True when it was actually sent."""
    if not settings.alerts_enabled:
        ctx.detail("Alerts disabled; email not sent.")
        return False
    if not settings.can_send_email:
        ctx.warn("ERROR_EMAIL_TO/EMAIL_SENDER/EMAIL_PASSWORD not configured; "
                 "alert email skipped")
        return False

    message = EmailMessage()
    message["From"] = settings.email_sender
    message["To"] = ", ".join(settings.error_email_to)
    message["Subject"] = (
        f"[Scraper FAILED] {record['process']} - {record['target_date']} "
        f"- {record['stage']}"
    )
    message.set_content(
        "This alert requires an HTML-capable mail client.\n\n"
        f"Process: {record['process']}\n"
        f"Target Date: {record['target_date']}\n"
        f"Stage: {record['stage']}\n"
        f"Error: {record['exception_type']}: {record['exception_message']}\n"
        + ("Locators tried:\n"
           + "".join(f"  {i}. {t}\n"
                     for i, t in enumerate(record.get("locators_tried") or [], 1))
           if record.get("locators_tried") else "")
    )
    message.add_alternative(render_alert_html(record), subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                          timeout=settings.smtp_timeout) as server:
            server.starttls()
            server.login(settings.email_sender, settings.email_password)
            server.send_message(message)
        ctx.fail(f"Alert emailed to {', '.join(settings.error_email_to)}")
        return True
    except Exception as exc:
        # An alert that cannot be delivered must not mask the original failure.
        ctx.fail(f"Could not send alert email: {exc}")
        return False


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------

class CentralErrorHandler:
    """Last stop for a failure that recovery could not fix."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.records = []

    def handle(self, exc, page=None, session=None, stage=None, attempts=None,
               alert=True):
        """Log with traceback, capture evidence, record history, email."""
        stage = stage or self.ctx.failed_stage or self.ctx.stage
        category, action, _ = decide(exc)

        # Four lines: what broke, why, where, and where the deep detail lives.
        # Enough to triage from the log alone; the full traceback, screenshot
        # and trace are in the evidence folder.
        self.ctx.fail(f"{STAGE_LABEL.get(stage, stage)} failed")
        self.ctx.fail(f"Reason: {_one_line(f'{type(exc).__name__}: {exc}')}")

        record = capture_evidence(
            self.ctx, exc, page=page, session=session, stage=stage, attempts=attempts
        )
        origin = record["origin"]
        if origin.get("file"):
            self.ctx.fail(
                f"Where: {self.ctx.relative(origin['file'])}:{origin['line']} "
                f"in {origin['function']}() | attempts: {attempts or 1}"
            )
        self.ctx.fail(
            f"Evidence: {self.ctx.relative(self.ctx.evidence_dir())}/ "
            f"(error.json"
            + (", screenshot.png" if record["screenshot_path"] else "")
            + (", trace.zip" if record["trace_path"] else "")
            + ")"
        )
        # Full traceback stays out of the daily log; DEBUG or error.json has it.
        self.ctx.logger.debug(record["traceback"])

        record["alert_sent"] = send_alert(self.ctx, record) if alert else False
        self.records.append(record)
        return record
