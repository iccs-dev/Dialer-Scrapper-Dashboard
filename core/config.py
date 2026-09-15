"""Runtime settings, all overridable from .env.

Nothing here is read at import time by anything other than this module, so a
test can set an environment variable and call ``reload()``.
"""

import os

import common

_TRUE = {"1", "true", "yes", "on"}


def _flag(name, default):
    return os.getenv(name, str(default)).strip().lower() in _TRUE


def _int(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


class Settings:
    """Snapshot of the environment-driven configuration."""

    def __init__(self):
        common.load_env()

        # --- retry / recovery ---
        self.max_retries = _int("SCRAPER_MAX_RETRIES", 2)
        self.retry_backoff_seconds = _int("SCRAPER_RETRY_BACKOFF", 5)
        self.max_browser_restarts = _int("SCRAPER_MAX_BROWSER_RESTARTS", 1)
        self.max_relogins = _int("SCRAPER_MAX_RELOGINS", 1)

        # --- logging / evidence ---
        self.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        self.trace_on_error = _flag("TRACE_ON_ERROR", True)
        self.screenshot_on_error = _flag("SCREENSHOT_ON_ERROR", True)

        # --- browser ---
        self.headless = _flag("SCRAPER_HEADLESS", True)
        self.nav_timeout_ms = _int("SCRAPER_NAV_TIMEOUT_MS", 30000)
        self.action_timeout_ms = _int("SCRAPER_ACTION_TIMEOUT_MS", 15000)
        self.locator_timeout_ms = _int("SCRAPER_LOCATOR_TIMEOUT_MS", 7000)
        self.download_timeout_ms = _int(
            "SCRAPER_DOWNLOAD_TIMEOUT_MS", common.DOWNLOAD_TIMEOUT_SECONDS * 1000
        )
        self.viewport_width = _int("SCRAPER_VIEWPORT_WIDTH", 1920)
        self.viewport_height = _int("SCRAPER_VIEWPORT_HEIGHT", 1080)

        # --- alerting ---
        self.error_email_to = _list("ERROR_EMAIL_TO", "digx.automation@iccs.in")
        self.email_sender = os.getenv("EMAIL_SENDER", "")
        self.email_password = os.getenv("EMAIL_PASSWORD", "")
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = _int("SMTP_PORT", 587)
        self.smtp_timeout = _int("SMTP_TIMEOUT", 60)
        self.alerts_enabled = _flag("SCRAPER_ALERTS_ENABLED", True)

    @property
    def can_send_email(self):
        return bool(self.email_sender and self.email_password and self.error_email_to)

    def redacted(self):
        """Everything safe to print. Credentials never appear."""
        skip = {"email_password"}
        out = {}
        for key, value in sorted(vars(self).items()):
            if key in skip:
                out[key] = "***" if value else "(unset)"
            else:
                out[key] = value
        return out


settings = Settings()


def reload():
    """Re-read the environment (used by tests)."""
    global settings
    settings = Settings()
    return settings
