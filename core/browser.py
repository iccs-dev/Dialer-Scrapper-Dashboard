"""Playwright session, resilient locators and controlled recovery.

Three things live here:

* :class:`BrowserSession` - launch/restart/close plus tracing and downloads;
* :func:`resolve` - try semantic locators first, fall back in a defined order,
  and say which strategy actually matched;
* :class:`RecoveryRunner` - run an action, and on failure apply the recovery
  the error category calls for (retry, re-locate, re-login, restart browser)
  before giving up and letting the central handler take over.
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from core.config import settings
from core.errors import Action, UiChangedError, decide
from core.runlog import Stage

#: Chromium flags mirroring the ones the Selenium version ran with.
DEFAULT_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-blink-features=AutomationControlled",
    "--allow-running-insecure-content",
    "--ignore-certificate-errors",
]


class BrowserSession:
    """Owns the Playwright objects for one run."""

    def __init__(self, ctx, download_dir=None, extra_args=(), headless=None):
        self.ctx = ctx
        self.download_dir = Path(download_dir) if download_dir else None
        self.extra_args = list(extra_args)
        self.headless = settings.headless if headless is None else headless
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._tracing = False
        self.restarts = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self.ctx.detail(f"Launching Chromium (headless={self.headless})")
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=DEFAULT_ARGS + self.extra_args,
        )
        self.context = self.browser.new_context(
            accept_downloads=True,
            ignore_https_errors=True,
            viewport={"width": settings.viewport_width, "height": settings.viewport_height},
        )
        self.context.set_default_timeout(settings.action_timeout_ms)
        self.context.set_default_navigation_timeout(settings.nav_timeout_ms)

        if settings.trace_on_error:
            try:
                self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
                self._tracing = True
            except Exception as exc:
                self.ctx.warn(f"Tracing unavailable: {exc}")

        self.page = self.context.new_page()
        self.ctx.detail(f"Chromium ready (version={getattr(self.browser, 'version', None)})")
        return self.page

    def restart(self):
        """Relaunch after a browser-level failure. Tracing restarts too."""
        self.restarts += 1
        self.ctx.warn(f"Browser restarted (#{self.restarts})")
        self.close(save_trace_to=None)
        return self.start()

    def stop_trace(self, path):
        """Stop tracing and write the archive. Returns the path, or None."""
        if not self._tracing or self.context is None:
            return None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.context.tracing.stop(path=str(path))
        finally:
            self._tracing = False
        return str(path)

    def close(self, save_trace_to=None):
        if self._tracing:
            try:
                if save_trace_to:
                    self.stop_trace(save_trace_to)
                else:
                    self.context.tracing.stop()
            except Exception:
                pass
            self._tracing = False
        for closer in (
            lambda: self.context and self.context.close(),
            lambda: self.browser and self.browser.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            try:
                closer()
            except Exception:
                pass
        self.context = self.browser = self._playwright = self.page = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


# ---------------------------------------------------------------------------
# Resilient locators
# ---------------------------------------------------------------------------

def resolve(scope, candidates, ctx, description, timeout_ms=None, state="visible"):
    """Return the first locator that matches, trying *candidates* in order.

    ``candidates`` is a list of ``(label, callable)`` pairs; the callable is
    given the scope (page or frame) and returns a Locator. Semantic strategies
    should come first and brittle ones (ids, XPath) last, so the scraper keeps
    working when the markup shifts underneath it.

    Raises :class:`UiChangedError` when nothing matched - which the policy
    treats as "alert, do not retry", because retrying a vanished element only
    wastes the run.
    """
    timeout_ms = timeout_ms or settings.locator_timeout_ms
    per_try = max(1000, timeout_ms // max(1, len(candidates)))
    tried = []

    for label, factory in candidates:
        tried.append(label)
        try:
            locator = factory(scope).first
            locator.wait_for(state=state, timeout=per_try)
        except Exception:
            ctx.detail(f"locator '{description}': {label} did not match")
            continue
        if label != candidates[0][0]:
            # Worth surfacing: the preferred strategy stopped working, which is
            # the early warning that the UI has drifted.
            ctx.warn(f"{description}: using fallback locator ({label})")
        else:
            ctx.detail(f"locator '{description}': matched via {label}")
        return locator

    raise UiChangedError(description, tried)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

class RecoveryRunner:
    """Runs an action and applies the recovery its failure category calls for."""

    def __init__(self, ctx, session=None, relogin=None):
        self.ctx = ctx
        self.session = session
        self.relogin = relogin
        self.browser_restarts = 0
        self.relogins = 0

    def run(self, description, action_fn, stage=None, max_retries=None):
        """Execute ``action_fn()``, recovering per policy. Re-raises on failure."""
        stage = stage or self.ctx.stage
        attempts_allowed = 1 + (settings.max_retries if max_retries is None else max_retries)
        last_exc = None

        for attempt in range(1, attempts_allowed + 1):
            try:
                if attempt > 1:
                    self.ctx.detail(f"{description}: attempt {attempt}/{attempts_allowed}")
                return action_fn()
            except Exception as exc:
                last_exc = exc
                category, recovery, retryable = decide(exc)
                self.ctx.warn(
                    f"Retry {attempt}/{attempts_allowed}: {description} ({category})"
                )
                self.ctx.detail(f"{description} failed: {type(exc).__name__}: {exc}")

                if not retryable:
                    self.ctx.detail(
                        f"{description}: {category} is not retryable ({recovery})"
                    )
                    raise
                if attempt >= attempts_allowed:
                    self.ctx.detail(
                        f"{description}: retries exhausted after {attempt} attempts"
                    )
                    raise

                if not self._recover(recovery, description, stage):
                    raise
                time.sleep(settings.retry_backoff_seconds)

        if last_exc:
            raise last_exc

    def _recover(self, recovery, description, stage):
        """Apply the recovery step. False means recovery is not possible."""
        if recovery == Action.RESTART_BROWSER:
            if self.session is None:
                self.ctx.detail("Browser restart needed but no session available")
                return False
            if self.browser_restarts >= settings.max_browser_restarts:
                self.ctx.detail(
                    f"Browser restart limit ({settings.max_browser_restarts}) reached"
                )
                return False
            self.browser_restarts += 1
            self.session.restart()
            if self.relogin:
                # A fresh browser has no session; log in before retrying.
                self.relogins += 1
                self.ctx.detail("Re-authenticating after browser restart")
                self.relogin()
            return True

        if recovery == Action.RELOGIN:
            if self.relogin is None:
                self.ctx.detail("Re-login needed but no callback provided")
                return False
            if self.relogins >= settings.max_relogins:
                self.ctx.detail(f"Re-login limit ({settings.max_relogins}) reached")
                return False
            self.relogins += 1
            self.ctx.warn("Session expired; logging in again")
            self.relogin()
            return True

        if recovery == Action.RELOCATE:
            self.ctx.detail(f"{description}: element went stale, re-locating")
            return True

        # Plain retry.
        return True
