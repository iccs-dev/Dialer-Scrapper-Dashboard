"""Shared, OS-independent configuration for the Dialer Scrapper scripts.

The scripts were written on Windows and relied on two things that do not hold
on Ubuntu:

* absolute Windows paths (``D:\\Rakshit\\refactoring\\Dialer_again``) and
  backslash-joined relative paths (``media\\Imagine\\APR_data``);
* a case-insensitive filesystem, so ``media`` and ``Media`` were the same
  directory.

All path handling therefore goes through this module, which resolves paths
relative to the project root and matches existing directories
case-insensitively so the same code runs unchanged on both platforms.

Browser automation lives in core/browser.py (Playwright); this module has no
browser dependency, so tooling such as hrms_monitor.py can import it anywhere.
"""

import datetime as _datetime
import os

# ==================== CONFIGURATION ====================
# Every folder and file name the project uses is defined here and nowhere else,
# so no script contains a literal path segment, folder name or date.

#: Environment variable overriding the auto-detected process name.
PROCESS_ENV_VAR = "DIALER_PROCESS"

#: Top-level data folder, relative to the project root.
MEDIA_FOLDER = "Media"

#: Date format for file names. Folders always use YYYY/MM/DD segments.
DATE_FORMAT = "%Y-%m-%d"

#: Suffix identifying the APR report inside a file name.
REPORT_TAG = "APR"

#: Per-process folders. Resolved case-insensitively against what is already on
#: disk, so "logs" reuses an existing "LOGs" and "Imagine" an existing "imagine".
#: Legacy single log folder, still recognised so existing files resolve.
FOLDER_LOGS = "Logs"
#: Logs are split by the report a script produces, so an APR run and a
#: Disposition run of the same process never share a file or a folder.
FOLDER_APR_LOGS = "APR_Logs"
FOLDER_DISPOSITION_LOGS = "Disposition_Logs"
FOLDER_APR_CLEAN_LOGS = "APR_Clean_logs"
FOLDER_APR_RAW = "APR_data"
FOLDER_APR_CLEAN = "APR_Clean"
FOLDER_CSV_COPY = "csv_data"
FOLDER_PROCESSED = "dialer_data"
FOLDER_UPLOAD = "upload"
FOLDER_FAILED_AGENTS = "Failed_Agent_list"

#: Failure evidence (screenshot, trace, error.json, history.jsonl) per run.
FOLDER_ERRORS = "errors"

#: Disposition report output, mirroring the APR folders.
FOLDER_DISPOSITION = "disposition_data"
FOLDER_CLEAN_DISPOSITION = "Clean_disposition"
FOLDER_CLEAN_DISPOSITION_DATA = "Clean_disposition_data"

#: Folders that belong to a process. Anything else directly under the media
#: root is a process folder.
DATASET_FOLDERS = (
    FOLDER_LOGS,
    FOLDER_APR_LOGS,
    FOLDER_DISPOSITION_LOGS,
    FOLDER_APR_CLEAN_LOGS,
    FOLDER_APR_RAW,
    FOLDER_APR_CLEAN,
    FOLDER_CSV_COPY,
    FOLDER_PROCESSED,
    FOLDER_UPLOAD,
    FOLDER_FAILED_AGENTS,
    FOLDER_ERRORS,
    FOLDER_DISPOSITION,
    FOLDER_CLEAN_DISPOSITION,
    FOLDER_CLEAN_DISPOSITION_DATA,
)

#: Process that owns the combined HRMS upload file. Shared by combine.py (which
#: writes it) and hrms.py (which reads it) so the two cannot drift apart.
UPLOAD_PROCESS = "hrms"

#: How long to wait for a report download to complete, in seconds. Used as the
#: default for SCRAPER_DOWNLOAD_TIMEOUT_MS in core/config.py; Playwright's
#: expect_download() does the actual waiting.
DOWNLOAD_TIMEOUT_SECONDS = 300

# ==================== PATHS ====================

#: Where the code lives. Always the folder holding this module, so process
#: detection stays correct even when the data root is relocated.
CODE_ROOT = os.path.dirname(os.path.abspath(__file__))

#: Data root. Override with DIALER_SCRAPPER_ROOT to keep the media tree
#: somewhere other than next to the code (a separate volume, or for testing).
PROJECT_ROOT = os.path.abspath(os.getenv("DIALER_SCRAPPER_ROOT") or CODE_ROOT)


def resolve_dir(parent, name):
    """Return ``parent/name``, reusing an existing directory that differs only
    in case (``Media`` vs ``media``). The path is not created."""
    candidate = os.path.join(parent, name)
    if os.path.isdir(candidate):
        return candidate
    try:
        entries = os.listdir(parent)
    except OSError:
        return candidate
    lowered = name.lower()
    for entry in entries:
        if entry.lower() == lowered and os.path.isdir(os.path.join(parent, entry)):
            return os.path.join(parent, entry)
    return candidate


MEDIA_DIR = resolve_dir(PROJECT_ROOT, MEDIA_FOLDER)


def as_date(value):
    """Accept a date, a datetime or a 'YYYY-MM-DD' string and return a date."""
    if isinstance(value, _datetime.datetime):
        return value.date()
    if isinstance(value, _datetime.date):
        return value
    return _datetime.datetime.strptime(str(value), DATE_FORMAT).date()


def date_parts(value):
    """('2026', '09', '07') for 2026-09-07 - the YYYY/MM/DD folder segments."""
    d = as_date(value)
    return (f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}")


def media_path(*parts, create=False):
    """Undated path inside the media tree. Mainly for tooling."""
    path = os.path.join(MEDIA_DIR, *parts)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def list_processes(exclude=()):
    """Process folders present under the media root, newest layout only."""
    if not os.path.isdir(MEDIA_DIR):
        return []
    skip = {name.lower() for name in DATASET_FOLDERS}
    skip |= {name.lower() for name in exclude}
    return sorted(
        name
        for name in os.listdir(MEDIA_DIR)
        if os.path.isdir(os.path.join(MEDIA_DIR, name)) and name.lower() not in skip
    )


def list_data_processes(exclude=()):
    """Processes that actually produce scraped data.

    A process owns a processed-output folder; the media folders that scripts
    create merely to hold their own logs (combine, hrms, Monitor) do not, so
    they are filtered out here rather than by name.
    """
    found = []
    for process in list_processes(exclude=exclude):
        if os.path.isdir(resolve_dir(os.path.join(MEDIA_DIR, process), FOLDER_PROCESSED)):
            found.append(process)
    return found


def detect_process(script_file):
    """Process a script belongs to, derived from where the script lives.

    A script inside a process folder (``Scrapper/Smart_Dial/Imagine/script.py``)
    belongs to that folder's process (``Imagine``). A script sitting at the
    project root has no parent process, so its own name is used (``hrms.py`` ->
    ``hrms``). Set DIALER_PROCESS to override.
    """
    override = os.getenv(PROCESS_ENV_VAR)
    if override:
        return override
    path = os.path.abspath(script_file)
    folder = os.path.dirname(path)
    # Compared against CODE_ROOT, not PROJECT_ROOT: relocating the data root
    # must not change which process a script belongs to.
    if os.path.normpath(folder) == os.path.normpath(CODE_ROOT):
        return os.path.splitext(os.path.basename(path))[0]
    return os.path.basename(folder)


def script_log_name(script_file):
    """Log file name for a script: ``Cleaning_Script.py`` -> ``cleaning_script.log``."""
    stem = os.path.splitext(os.path.basename(os.path.abspath(script_file)))[0]
    return f"{stem.lower()}.log"


class ProcessPaths:
    """Every path for one process on one date.

    The layout is the same for data and logs, and each folder is created on
    first use::

        Media/<process>/<folder>/<YYYY>/<MM>/<DD>/
        Media/Imagine/APR_data/2026/09/07/2026-09-07_APR.csv
        Media/Imagine/logs/2026/09/07/script.log
    """

    def __init__(self, process, date):
        self.process = process
        self.date = as_date(date)

    def __repr__(self):
        return f"ProcessPaths(process={self.process!r}, date={self.date_key!r})"

    @property
    def date_key(self):
        """The date as it appears in file names, e.g. '2026-09-07'."""
        return self.date.strftime(DATE_FORMAT)

    @property
    def root(self):
        """Media/<process>."""
        return resolve_dir(MEDIA_DIR, self.process)

    def dataset(self, folder, create=True):
        """Media/<process>/<folder>/<YYYY>/<MM>/<DD>, created on demand."""
        path = os.path.join(resolve_dir(self.root, folder), *date_parts(self.date))
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    def file(self, folder, filename, create=True):
        """A file inside the dated folder for this process."""
        return os.path.join(self.dataset(folder, create=create), filename)

    def report_name(self, extension, tag=REPORT_TAG):
        """'2026-09-07_APR.csv' for extension='csv'."""
        return f"{self.date_key}_{tag}.{extension.lstrip('.')}"

    def dated_name(self, extension):
        """'2026-09-07.csv' for extension='csv'."""
        return f"{self.date_key}.{extension.lstrip('.')}"

    def logs(self, create=True, folder=None):
        """Media/<process>/<log folder>/<YYYY>/<MM>/<DD>.

        `folder` names which report's logs these are; it defaults to the APR
        workflow because that is what the scraper, combine and HRMS produce.
        """
        return self.dataset(folder or FOLDER_APR_LOGS, create=create)

    def log_file(self, name, create=True):
        """A log file inside this process's dated log folder."""
        return os.path.join(self.logs(create=create), name)


#: Which log folder a script writes to, by file stem (lower-cased). A stem
#: ending in "_disposition" is a Disposition report for its process; anything
#: else belongs to the APR workflow.
LOG_FOLDER_BY_STEM = {
    "script": FOLDER_APR_LOGS,
    "cleaning_script": FOLDER_APR_LOGS,
    "combine": FOLDER_APR_LOGS,
    "hrms": FOLDER_APR_LOGS,
    "run_scrapers": FOLDER_APR_LOGS,
}


def log_folder(script_file):
    """The log folder for the script that is running.

    Keeps the APR and Disposition trees completely separate: script.py writes
    under APR_Logs, <process>_Disposition.py under Disposition_Logs, and
    neither can reach the other's directory.
    """
    stem = os.path.splitext(os.path.basename(str(script_file)))[0].lower()
    if stem.endswith("_disposition") or stem == "disposition":
        return FOLDER_DISPOSITION_LOGS
    return LOG_FOLDER_BY_STEM.get(stem, FOLDER_APR_LOGS)


def process_paths(script_file, date, process=None):
    """ProcessPaths for the calling script and a report date."""
    return ProcessPaths(process or detect_process(script_file), date)


def summarise_reasons(entries, limit=2):
    """Group (reason, id) pairs into a readable one-line summary.

    ``[("Already processed", "ATS1"), ("Already processed", "ATS2")]`` ->
    ``"2 Already processed (ATS1, ATS2)"``. Shared by combine.py and
    hrms_monitor.py so the wording matches wherever rejections are reported.
    """
    grouped = {}
    for reason, item in entries:
        grouped.setdefault(reason or "Rejected", []).append(item)
    parts = []
    for reason, items in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        sample = ", ".join(items[:limit])
        if len(items) > limit:
            sample += f" +{len(items) - limit}"
        parts.append(f"{len(items)} {reason} ({sample})")
    return "; ".join(parts)


def load_env():
    """Load the project's .env file (no-op if python-dotenv is missing)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
