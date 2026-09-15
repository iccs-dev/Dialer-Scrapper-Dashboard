"""One combined daily log per process for the whole workflow.

    Media/<Process>/Logs/<YYYY>/<MM>/<DD>/<Process>_<YYYY-MM-DD>.log

Scraper, Combine and HRMS all append to the same file, so a day's workflow can
be read top to bottom from a single place. The file carries a status summary
above the detail lines::

    ==================================================
    Imagine Daily Process Log
    Date: 2026-09-14
    ==================================================

    SCRAPER : SUCCESS
    COMBINE : FAILED
    HRMS    : NOT RUN

    --------------------------------------------------
    DETAILS
    --------------------------------------------------

    [10:31:00] [COMBINE] START
    [10:31:05] [COMBINE] ERROR - Input file not found
    [10:31:05] [COMBINE] FAILED
    [10:31:05] [HRMS] NOT RUN - Combine failed

The summary has to sit above details that are written later, so each update
rewrites the file: the current statuses are read back from the summary block
itself (no sidecar state file), the changed status is applied, and header plus
details are written out again. Files are small - one day of one process - so
this stays cheap, and it keeps everything in the single file that was asked
for.
"""

import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import common

HEADER_RULE = "=" * 50
SECTION_RULE = "-" * 50
DETAILS_HEADING = "DETAILS"

#: The workflow, in order. Each one gets a line in the summary.
SCRAPER = "SCRAPER"
COMBINE = "COMBINE"
HRMS = "HRMS"
DISPOSITION = "DISPOSITION"

#: The APR workflow, in order. Each stage gets a line in the summary block.
WORKFLOW = (SCRAPER, COMBINE, HRMS)

#: A Disposition run is a single stage and has nothing to do with the upload
#: to HRMS, so its file carries its own one-line summary instead of three
#: stages that would sit at NOT RUN forever.
WORKFLOW_DISPOSITION = (DISPOSITION,)

#: Log folder -> the workflow its summary block describes.
WORKFLOW_BY_FOLDER = {
    common.FOLDER_APR_LOGS: WORKFLOW,
    common.FOLDER_LOGS: WORKFLOW,
    common.FOLDER_DISPOSITION_LOGS: WORKFLOW_DISPOSITION,
}

#: Statuses a stage can hold.
NOT_RUN = "NOT RUN"
RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
FAILED = "FAILED"
TERMINAL = (SUCCESS, FAILED)

_ALL_STAGES = WORKFLOW + WORKFLOW_DISPOSITION
_STATUS_LINE = re.compile(r"^(" + "|".join(_ALL_STAGES) + r")\s*:\s*(.+?)\s*$")

#: How long a lock file may sit before it is treated as abandoned.
LOCK_STALE_SECONDS = 60


@contextmanager
def _lock(path):
    """Cross-platform advisory lock.

    combine.py runs hrms.py as a subprocess while it is still logging, so two
    processes can rewrite the same file at once. O_EXCL works the same on Linux
    and Windows, unlike fcntl/msvcrt.
    """
    lock_path = Path(str(path) + ".lock")
    handle = None
    for _ in range(200):
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    try:
        yield
    finally:
        if handle is not None:
            try:
                os.close(handle)
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


class DailyLog:
    """The single daily log file for one process."""

    def __init__(self, process, date, folder=None):
        self.process = process
        self.date = common.as_date(date)
        self.date_key = self.date.strftime(common.DATE_FORMAT)
        # APR and Disposition keep separate trees, so a process running both
        # reports never has the two interleaved in one file.
        self.folder_name = folder or common.FOLDER_APR_LOGS
        self.workflow = WORKFLOW_BY_FOLDER.get(self.folder_name, WORKFLOW)
        folder = Path(
            common.ProcessPaths(process, self.date).dataset(
                self.folder_name, create=True
            )
        )
        self.path = folder / f"{process}_{self.date_key}.log"

    # -- file shape ---------------------------------------------------------

    def _blank_statuses(self):
        return {stage: NOT_RUN for stage in self.workflow}

    def _read(self):
        """(statuses, details) from the file, or empty defaults."""
        if not self.path.is_file():
            return self._blank_statuses(), ""
        text = self.path.read_text(encoding="utf-8", errors="replace")

        statuses = self._blank_statuses()
        head, _, rest = text.partition(DETAILS_HEADING)
        for line in head.splitlines():
            found = _STATUS_LINE.match(line)
            if found and found.group(1) in statuses:
                statuses[found.group(1)] = found.group(2)

        details = ""
        if rest:
            # Drop the rule line that closes the DETAILS heading.
            _, _, after = rest.partition(SECTION_RULE)
            details = after.lstrip("\n")
        return statuses, details

    def _render(self, statuses, details):
        summary = "\n".join(f"{stage:<8}: {statuses[stage]}"
                            for stage in self.workflow)
        return (
            f"{HEADER_RULE}\n"
            f"{self.process} Daily Process Log\n"
            f"Date: {self.date_key}\n"
            f"{HEADER_RULE}\n\n"
            f"{summary}\n\n"
            f"{SECTION_RULE}\n"
            f"{DETAILS_HEADING}\n"
            f"{SECTION_RULE}\n\n"
            f"{details}"
        )

    def _update(self, status=None, lines=(), separator=False):
        """Apply a status change and/or append detail lines, atomically."""
        with _lock(self.path):
            statuses, details = self._read()
            if status:
                stage, value = status
                # Stages outside the three-stage workflow (e.g. CLEANING) show
                # in DETAILS but do not get a summary line.
                if stage in statuses:
                    statuses[stage] = value
            new = list(lines)
            if separator:
                stamp = datetime.now().strftime("%H:%M:%S")
                block = f"{HEADER_RULE}\nRUN STARTED: {stamp}\n{HEADER_RULE}\n"
                details = (details.rstrip("\n") + "\n\n" if details.strip() else "") + block
            if new:
                if details and not details.endswith("\n"):
                    details += "\n"
                details += "\n".join(new) + "\n"
            self.path.write_text(self._render(statuses, details), encoding="utf-8")

    # -- writing ------------------------------------------------------------

    @staticmethod
    def _line(stage, message):
        return f"[{datetime.now():%H:%M:%S}] [{stage}] {message}"

    def begin(self, stage):
        """Mark a stage as running and open its block in DETAILS."""
        statuses, details = self._read()
        # A repeat of a stage that already finished today is a new run, and so
        # is a fresh SCRAPER pass - which resets what follows it.
        repeat = statuses.get(stage) in TERMINAL
        fresh_cycle = stage == self.workflow[0] and any(
            statuses[s] in TERMINAL for s in self.workflow
        )
        separator = repeat or fresh_cycle

        if fresh_cycle:
            with _lock(self.path):
                _, current_details = self._read()
                self.path.write_text(
                    self._render(self._blank_statuses(), current_details),
                    encoding="utf-8",
                )

        lines = []
        if details.strip() and not separator:
            lines.append("")  # blank line between process blocks
        lines.append(self._line(stage, "START"))
        self._update(status=(stage, RUNNING), lines=lines, separator=separator)

    def step(self, stage, message):
        self._update(lines=[self._line(stage, message)])

    def error(self, stage, message):
        self._update(lines=[self._line(stage, f"ERROR - {message}")])

    def succeeded(self, stage):
        self._update(status=(stage, SUCCESS), lines=[self._line(stage, SUCCESS)])

    def failed(self, stage, reason=None):
        """Mark the stage failed and every later stage as skipped."""
        self._update(status=(stage, FAILED), lines=[self._line(stage, FAILED)])
        if stage in self.workflow:
            skipped = self.workflow[self.workflow.index(stage) + 1:]
            for index, later in enumerate(skipped):
                # Blank line before the skipped block, matching the way each
                # process block is separated.
                self.not_run(later, f"{stage.title()} failed", spaced=index == 0)

    def not_run(self, stage, reason, spaced=True):
        """Record explicitly that a stage was skipped, and why.

        Written without a timestamp: nothing happened at a point in time, the
        stage simply never ran.
        """
        lines = [""] if spaced else []
        lines.append(f"[{stage}] {NOT_RUN} - {reason}")
        self._update(status=(stage, NOT_RUN), lines=lines)

    def __repr__(self):
        return f"DailyLog({self.process!r}, {self.date_key!r})"


class DailyLogGroup:
    """Writes the same workflow to the daily log of every process involved.

    With one process this is just Imagine's log; when more scrapers are added,
    Combine and HRMS lines land in each contributing process's daily log, so
    every process's file still tells its whole story.
    """

    def __init__(self, processes, date, folder=None):
        seen = []
        for process in processes:
            if process and process not in seen:
                seen.append(process)
        self.logs = [DailyLog(p, date, folder=folder) for p in seen]

    def __bool__(self):
        return bool(self.logs)

    @property
    def paths(self):
        return [log.path for log in self.logs]

    def _fan(self, method, *args, only=None):
        """Run `method` on every log, or on just one process's log.

        `only` exists because some lines are facts about one process -
        "ICAI: no data" - and writing those to every contributing process's
        file mixes the processes together. A process's log must only ever
        describe that process.
        """
        for log in self.logs:
            if only is not None and log.process != only:
                continue
            try:
                getattr(log, method)(*args)
            except Exception:
                # The daily log is a report, never a reason to fail a run.
                pass

    def begin(self, stage):
        self._fan("begin", stage)

    def step(self, stage, message, only=None):
        self._fan("step", stage, message, only=only)

    def error(self, stage, message, only=None):
        self._fan("error", stage, message, only=only)

    def succeeded(self, stage):
        self._fan("succeeded", stage)

    def failed(self, stage, reason=None):
        self._fan("failed", stage, reason)

    def not_run(self, stage, reason):
        self._fan("not_run", stage, reason)
