"""Human-readable, per-run scraper logs.

A run reads as a short story rather than a machine feed::

    2026-09-08 10:20:02 | [START] Imagine

    [LOGIN] Success
    [NAVIGATION] Report page ready
    [SCRAPING] Campaigns: 13/13 | Users: 423/423
    [DATE] 2026-09-07 -> 2026-09-08
    [DOWNLOAD] user_session_DYSON.xls
    [VALIDATION] 2033 rows | 35 columns
    [PROCESSING] CSV created | Net Login added
    [PROCESSING] Final rows: 23

    [SUCCESS] Completed in 37.83s

Only the [START] line is timestamped and the closing line carries the duration,
so the envelope is known without stamping every step. Level, run id, target
date and process are not repeated per line either - they are already in the
file name and the folder path::

    Media/<process>/logs/<YYYY>/<MM>/<DD>/<script>_<timestamp>_<run_id>.log

Implementation detail (locator strategies, browser version, URLs, absolute
paths) is logged at DEBUG, so LOG_LEVEL=DEBUG still gives a full trace when
debugging without cluttering the daily log.
"""

import logging
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import common
from core.config import settings
from core.daily_log import COMBINE, HRMS, SCRAPER, DailyLogGroup

try:
    import monitor
except ImportError:            # dashboard module absent - scrapers still run
    monitor = None


class Stage:
    """Named points in a run. Used for log tags and error attribution."""

    STARTUP = "startup"
    BROWSER_START = "browser_start"
    LOGIN = "login"
    NAVIGATION = "navigation"
    DATE_SELECTION = "date_selection"
    SCRAPING = "scraping"
    DOWNLOAD = "download"
    VALIDATION = "validation"
    PROCESSING = "processing"
    HRMS_UPLOAD = "hrms_upload"
    COMPLETED = "completed"

    ALL = (
        STARTUP, BROWSER_START, LOGIN, NAVIGATION, DATE_SELECTION, SCRAPING,
        DOWNLOAD, VALIDATION, PROCESSING, HRMS_UPLOAD, COMPLETED,
    )


class Tag:
    """Every prefix that can appear in a log."""

    START = "START"
    LOGIN = "LOGIN"
    NAVIGATION = "NAVIGATION"
    SCRAPING = "SCRAPING"
    DATE = "DATE"
    DOWNLOAD = "DOWNLOAD"
    VALIDATION = "VALIDATION"
    PROCESSING = "PROCESSING"
    UPLOAD = "UPLOAD"
    DISPOSITION = "DISPOSITION"
    WARN = "WARN"
    ERROR = "ERROR"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


#: Default tag for a message logged while a stage is active.
STAGE_TAGS = {
    Stage.STARTUP: Tag.START,
    Stage.BROWSER_START: Tag.START,
    Stage.LOGIN: Tag.LOGIN,
    Stage.NAVIGATION: Tag.NAVIGATION,
    Stage.DATE_SELECTION: Tag.DATE,
    Stage.SCRAPING: Tag.SCRAPING,
    Stage.DOWNLOAD: Tag.DOWNLOAD,
    Stage.VALIDATION: Tag.VALIDATION,
    Stage.PROCESSING: Tag.PROCESSING,
    Stage.HRMS_UPLOAD: Tag.UPLOAD,
    Stage.COMPLETED: Tag.SUCCESS,
}

#: Messages arrive already formatted as "[TAG] text".
LOG_FORMAT = "%(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Which workflow stage each script is. Scripts absent from this map (the
#: run_scrapers orchestrator) write to the console only.
WORKFLOW_STAGE = {
    "script": SCRAPER,
    "cleaning_script": "CLEANING",
    "imagine_disposition": "DISPOSITION",
    "combine": COMBINE,
    "hrms": HRMS,
}


class RunContext:
    """Identity, log file and stage tracking for a single run."""

    def __init__(self, script_file, target_date, process=None, run_id=None,
                 daily_processes=None, log_folder=None, workflow_stage=None):
        self.script_file = str(script_file)
        self.paths = common.process_paths(script_file, target_date, process=process)
        self.process = self.paths.process
        self.target_date = self.paths.date
        self.date_key = self.paths.date_key
        # The dashboard sets DIALER_RUN_ID when it launches a script, so the
        # row it already created and the one this run reports to the
        # monitoring API are the same row. Without it each dashboard-triggered
        # run appears twice: once from the runner, once from the scraper.
        self.run_id = run_id or os.getenv("DIALER_RUN_ID", "").strip() \
            or uuid.uuid4().hex[:8]
        self.started_at = datetime.now()
        self.stage = Stage.STARTUP
        #: Innermost stage that raised, so error attribution names where the run
        #: actually broke rather than whatever stage was current after unwinding.
        self.failed_stage = None
        self._closed = False
        #: Number of [ERROR] lines emitted, so a run cannot log a failure and
        #: then close with [SUCCESS].
        self.error_count = 0
        #: Row counts reported to the dashboard; set via set_records().
        self.records = {"scraped": None, "uploaded": None, "failed": None}

        #: 'script', 'cleaning_script', 'combine', 'hrms', 'run_scrapers'.
        self.script_name = Path(self.script_file).stem.lower()
        #: SCRAPER / COMBINE / HRMS / CLEANING, or None for the orchestrator.
        # A cleaner is named script.py like a scraper, so its stage and log
        # folder cannot be inferred from the filename - the caller says.
        self.workflow_stage = workflow_stage or WORKFLOW_STAGE.get(self.script_name)
        if self.workflow_stage is None and self.script_name.endswith("_disposition"):
            # Every process names its report file <Process>_Disposition.py, so
            # match the suffix rather than listing each one.
            self.workflow_stage = "DISPOSITION"

        # One combined daily log per process, shared by all three stages.
        # No per-run file is written: the console shows the run, the daily log
        # is the record.
        # An empty list (no data processes on disk yet) must still produce a
        # log, so fall back to this script's own process.
        targets = daily_processes or [self.process]
        # script.py -> APR_Logs, <process>_Disposition.py -> Disposition_Logs.
        # Derived from the running script, never hardcoded per process.
        self.log_folder = log_folder or common.log_folder(self.script_file)
        self.daily = DailyLogGroup(targets if self.workflow_stage else [],
                                   self.target_date, folder=self.log_folder)
        self.log_path = self.daily.paths[0] if self.daily.paths else None

        # A unique logger name per run, and handlers added once, so a message
        # is never emitted twice by a second initialisation.
        self.logger = logging.getLogger(f"{self.process}.{self.script_name}.{self.run_id}")
        self.logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
        self.logger.propagate = False
        if not self.logger.handlers:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(logging.Formatter(LOG_FORMAT))
            self.logger.addHandler(console)

    # -- stage tracking -----------------------------------------------------

    def set_stage(self, stage):
        self.stage = stage
        return stage

    @contextmanager
    def stage_scope(self, stage, announce=False):
        """Make `stage` active for a block.

        Entering a stage logs nothing: a stage announces itself through the
        result it reports, which is what keeps the log free of "--> navigation"
        noise.
        """
        previous = self.stage
        self.stage = stage
        try:
            yield self
        except Exception:
            if self.failed_stage is None:
                self.failed_stage = stage
            raise
        finally:
            self.stage = previous

    # -- the log ------------------------------------------------------------

    def _emit(self, level, text):
        self.logger.log(level, text)

    def step(self, message, tag=None, stage=None, only=None):
        """One business fact: '[SCRAPING] Campaigns: 13/13 | Users: 423/423'.

        `only` names the single process a line belongs to. combine.py and
        hrms.py write into several processes' daily logs at once, so a line
        about one of them has to say which, or every file ends up describing
        every process.
        """
        tag = tag or STAGE_TAGS.get(stage or self.stage, Tag.START)
        self._emit(logging.INFO, f"[{tag}] {message}")
        self._daily(f"{tag} - {message}" if message else tag, only=only)
        self._monitor("log", message=f"{tag} - {message}" if message else tag,
                      stage=stage or self.stage, level="INFO")

    def warn(self, message, tag=Tag.WARN, only=None):
        self._emit(logging.WARNING, f"[{tag}] {message}")
        self._daily(f"WARN - {message}", only=only)
        self._monitor("log", message=message, stage=self.stage, level="WARN")

    def fail(self, message, only=None):
        self.error_count += 1
        self._emit(logging.ERROR, f"[{Tag.ERROR}] {message}")
        if self.daily and self.workflow_stage:
            self.daily.error(self.workflow_stage, message, only=only)
        self._monitor("log", message=message, stage=self.stage, level="ERROR")

    def _daily(self, message, only=None):
        """Mirror a detail line into the combined daily log."""
        if self.daily and self.workflow_stage:
            self.daily.step(self.workflow_stage, message, only=only)

    def detail(self, message):
        """Implementation detail; only shown when LOG_LEVEL=DEBUG."""
        self._emit(logging.DEBUG, f"[DEBUG] {message}")

    def blank(self):
        self._emit(logging.INFO, "")

    # Aliases so the non-browser scripts share one consistent format.
    debug = detail

    def info(self, message, stage=None, only=None):
        self.step(message, stage=stage, only=only)

    def warning(self, message, stage=None, only=None):
        self.warn(message, only=only)

    def error(self, message, stage=None, only=None):
        self.fail(message, only=only)

    def critical(self, message, stage=None):
        self.fail(message)

    def exception(self, message, stage=None):
        """Traceback goes to DEBUG; error.json always keeps the complete one."""
        self.fail(message)
        self.logger.debug("", exc_info=True)

    # -- run boundaries -----------------------------------------------------

    @property
    def script_relpath(self):
        """This script's path relative to the project root, for the dashboard."""
        try:
            return str(Path(self.script_file).resolve()
                       .relative_to(Path(common.CODE_ROOT).resolve()))
        except (ValueError, OSError):
            return ""

    def set_records(self, scraped=None, uploaded=None, failed=None):
        """Row counts to report when the run finishes."""
        if scraped is not None:
            self.records["scraped"] = scraped
        if uploaded is not None:
            self.records["uploaded"] = uploaded
        if failed is not None:
            self.records["failed"] = failed

    def _monitor(self, action, **kwargs):
        """Mirror a run event to the dashboard. Never affects the scrape."""
        if monitor is None or not monitor.is_enabled():
            return
        try:
            getattr(monitor, action)(
                self.process, run_date=self.date_key, run_id=self.run_id,
                script_path=self.script_relpath, **kwargs)
        except Exception:
            pass          # monitor.* already swallows; this is belt and braces

    def start(self):
        stamp = self.started_at.strftime(LOG_DATEFMT)
        self._emit(logging.INFO, f"{stamp} | [{Tag.START}] {self.process}")
        self.blank()
        if self.daily and self.workflow_stage:
            self.daily.begin(self.workflow_stage)
        self._monitor("start")

    def success(self, message=None):
        """The single closing line of a good run.

        Refuses to claim success if any [ERROR] was logged, so the closing line
        always matches what the run actually did.
        """
        if self._closed:
            return
        if self.error_count:
            self.failed(f"{self.process} ({self.error_count} error(s))")
            return
        self._closed = True
        self.stage = Stage.COMPLETED
        self.blank()
        self._emit(
            logging.INFO,
            f"[{Tag.SUCCESS}] {message or f'Completed in {self.elapsed_seconds:.2f}s'}",
        )
        if self.daily and self.workflow_stage:
            self.daily.succeeded(self.workflow_stage)
        self._monitor("success", records=self.records["scraped"],
                      uploaded=self.records["uploaded"],
                      failed_records=self.records["failed"])
        self._flush()

    def failed(self, message=None):
        """The single closing line of a bad run."""
        if self._closed:
            return
        self._closed = True
        self.stage = Stage.COMPLETED
        self.blank()
        self._emit(logging.ERROR, f"[{Tag.FAILED}] {message or self.process}")
        if self.daily and self.workflow_stage:
            # Marks this stage FAILED and every later stage NOT RUN, with the
            # reason, so a skipped stage is never mistaken for a broken one.
            self.daily.failed(self.workflow_stage)
        self._monitor("failed", error=message or f"{self.process} failed")
        self._flush()

    def finish(self, status="completed"):
        """Back-compat entry point. Closing twice is a no-op, so a run can
        never end with both a SUCCESS and a FAILED line."""
        if status == "completed":
            self.success()
        else:
            self.failed()

    def _flush(self):
        for handler in list(self.logger.handlers):
            handler.flush()

    # -- evidence location --------------------------------------------------

    def evidence_dir(self, create=True):
        """Media/<process>/errors/<YYYY>/<MM>/<DD>/<run_id>/"""
        path = Path(self.paths.dataset(common.FOLDER_ERRORS, create=create)) / self.run_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def history_path(self):
        """Append-only error history for this process and date."""
        return Path(self.paths.dataset(common.FOLDER_ERRORS)) / "history.jsonl"

    def relative(self, path):
        """Paths in a log are shown relative to the project, never absolute.

        Tries the data root as well as the code root, because
        DIALER_SCRAPPER_ROOT can put the media tree outside the checkout.
        """
        try:
            resolved = Path(path).resolve()
        except (OSError, TypeError):
            return str(path)
        for root in (common.PROJECT_ROOT, common.CODE_ROOT):
            try:
                return str(resolved.relative_to(Path(root).resolve()))
            except (ValueError, OSError):
                continue
        # Outside every known root: show just enough to find it.
        return os.path.join(*resolved.parts[-4:]) if len(resolved.parts) > 4 else str(resolved)

    @property
    def elapsed_seconds(self):
        return (datetime.now() - self.started_at).total_seconds()

    def __repr__(self):
        return (f"RunContext(process={self.process!r}, date={self.date_key!r}, "
                f"run_id={self.run_id!r})")


def start_run(script_file, target_date, process=None, daily_processes=None,
              log_folder=None, workflow_stage=None):
    """Create a RunContext and write the [START] banner."""
    ctx = RunContext(script_file, target_date, process=process,
                     daily_processes=daily_processes,
                     log_folder=log_folder, workflow_stage=workflow_stage)
    ctx.start()
    ctx.detail(f"script={Path(ctx.script_file).name} run_id={ctx.run_id} "
               f"target_date={ctx.date_key} log={ctx.log_path}")
    return ctx
