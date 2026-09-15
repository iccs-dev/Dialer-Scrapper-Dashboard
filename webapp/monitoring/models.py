"""Database models for the Dialer Scraper monitoring dashboard.

Ye models koi nayi cheez invent nahi karte - jo data scrapers already
Media/<Process>/Logs/... aur errors/history.jsonl mein likhte hain, wahi
database mein structured form mein aata hai:

    daily log  [SCRAPER] SUCCESS          -> ProcessRun.status
    [PROCESSING] Final rows: 22           -> ProcessRun.records_scraped
    [UPLOAD] Validated: 23 | 0 rejected   -> records_uploaded / records_failed
    errors/history.jsonl                  -> ProcessRun.error + ProcessLog
    daily log DETAILS lines               -> ProcessLog

Naya sirf heartbeat hai (requirement 11), jisse crash hua scraper hamesha
"Running" dikhta na rahe.
"""

from datetime import timedelta

from django.db import models
from django.utils import timezone


class Status(models.TextChoices):
    """Requirement 10 ke exact statuses."""

    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    SUCCESS = "SUCCESS", "Success"
    WARNING = "WARNING", "Warning"
    FAILED = "FAILED", "Failed"
    NOT_RESPONDING = "NOT_RESPONDING", "Not Responding"

    @classmethod
    def terminal(cls):
        """Jo statuses final hain - inke baad run aage nahi badhta."""
        return {cls.SUCCESS, cls.WARNING, cls.FAILED}


class ScheduleType(models.TextChoices):
    """Requirement 14: har process ka intended behaviour preserve karna hai."""

    SCHEDULED = "SCHEDULED", "Scheduled"        # systemd timer se roz chalega
    MANUAL = "MANUAL", "Manually triggered"     # dashboard ke Run button se
    CONTINUOUS = "CONTINUOUS", "Continuous"     # hamesha chalta rahe
    ONE_TIME = "ONE_TIME", "One-time"


class ProcessKind(models.TextChoices):
    """What a roster entry actually is.

    The two are run very differently: a scraper is selected and launched on
    its own, while a workflow step runs after the scrapers for a date have
    finished and turns their output into an HRMS upload. Without the
    distinction the runner would either verify an output file that a workflow
    step never produces, or offer "hrms" in the sidebar as if it scraped a
    dialer.
    """

    SCRAPER = "SCRAPER", "Scraper"
    WORKFLOW = "WORKFLOW", "Workflow step"


class Stage(models.TextChoices):
    """core/runlog.py ki Stage vocabulary - wahi naam, taaki logs match karein."""

    STARTUP = "startup", "Startup"
    BROWSER_START = "browser_start", "Browser start"
    LOGIN = "login", "Login"
    NAVIGATION = "navigation", "Navigation"
    DATE_SELECTION = "date_selection", "Date selection"
    SCRAPING = "scraping", "Scraping"
    DOWNLOAD = "download", "Download"
    VALIDATION = "validation", "Validation"
    PROCESSING = "processing", "Processing"
    HRMS_UPLOAD = "hrms_upload", "HRMS upload"
    COMPLETED = "completed", "Completed"


class Process(models.Model):
    """Ek scraper process - jaise Imagine, ICAI.

    `name` wahi hona chahiye jo common.detect_process() deta hai, kyunki
    scrapers usi naam se monitor API ko report karenge.
    """

    name = models.CharField(max_length=64, unique=True)
    category = models.CharField(max_length=64, blank=True, default="")
    kind = models.CharField(
        max_length=16, choices=ProcessKind.choices, default=ProcessKind.SCRAPER
    )
    schedule_type = models.CharField(
        max_length=16, choices=ScheduleType.choices, default=ScheduleType.SCHEDULED
    )

    # These four mirror the Streamlit dashboard's PROCESS_META exactly, so the
    # existing roster (including "Imagine Clean" / "Imagine_Disposition" as
    # separate processes) migrates without reinterpretation.
    #: Directory the script runs from, relative to the project root.
    working_dir = models.CharField(max_length=255, blank=True, default="")
    #: Script filename inside working_dir, e.g. "script.py".
    script_name = models.CharField(max_length=64, blank=True, default="script.py")
    #: Output folder relative to Media/, e.g. "Imagine/dialer_data".
    output_dir = models.CharField(max_length=255, blank=True, default="")
    #: Output filename template, e.g. "{date}_APR.csv".
    file_pattern = models.CharField(max_length=128, blank=True, default="{date}_APR.csv")
    #: True  -> script accepts ["start", "end"] and handles the range itself.
    #: False -> the runner loops one date at a time.
    range_aware = models.BooleanField(default=False)

    #: False when the script does not exist on this machine. Kept in the list
    #: so the UI stays familiar, but shown as unavailable.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def script_path(self):
        """Path to the runnable script, relative to the project root.

        combine.py and hrms.py sit at the project root, so an empty
        working_dir is a valid location, not a missing one - returning ""
        for them made the runner report "script not configured".
        """
        if not self.script_name:
            return ""
        if not self.working_dir:
            return self.script_name
        return f"{self.working_dir.rstrip('/')}/{self.script_name}"

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "processes"

    def __str__(self):
        return self.name


class ProcessRun(models.Model):
    """Ek process ka ek din ka ek run.

    Ek hi process ek din mein do baar chal sakta hai (re-run), isliye
    unique constraint (process, run_date) par NAHI hai - har attempt alag row.
    `run_id` core/runlog.py wali run id hai, jisse logs aur error evidence
    folder se link ho jaata hai.
    """

    process = models.ForeignKey(Process, on_delete=models.CASCADE, related_name="runs")
    run_date = models.DateField(help_text="Report date (usually yesterday)")
    run_id = models.CharField(max_length=32, blank=True, default="", db_index=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    records_scraped = models.IntegerField(default=0)
    records_uploaded = models.IntegerField(default=0)
    records_failed = models.IntegerField(default=0)

    error = models.TextField(blank=True, default="")
    #: Media/<process>/errors/<Y>/<M>/<D>/<run_id>/ - screenshot, trace, error.json
    evidence_path = models.CharField(max_length=255, blank=True, default="")

    #: Requirement 11 - scraper har 30s par isko update karta hai.
    last_heartbeat = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-run_date", "-started_at", "-id"]
        indexes = [
            models.Index(fields=["run_date", "status"]),
            models.Index(fields=["process", "run_date"]),
        ]

    def __str__(self):
        return f"{self.process.name} {self.run_date} [{self.status}]"

    # -- duration ----------------------------------------------------------

    @property
    def duration_seconds(self):
        """Run kitni der chala. Abhi chal raha hai to ab tak ka time."""
        if not self.started_at:
            return None
        end = self.completed_at or timezone.now()
        return (end - self.started_at).total_seconds()

    @property
    def duration_display(self):
        seconds = self.duration_seconds
        if seconds is None:
            return "-"
        if seconds < 60:
            return f"{seconds:.0f}s"
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"

    # -- heartbeat ---------------------------------------------------------

    def is_stale(self, timeout_seconds):
        """RUNNING hai par heartbeat timeout se purana? => Not Responding."""
        if self.status != Status.RUNNING:
            return False
        if self.last_heartbeat is None:
            reference = self.started_at
        else:
            reference = self.last_heartbeat
        if reference is None:
            return False
        return timezone.now() - reference > timedelta(seconds=timeout_seconds)

    def mark_not_responding(self):
        """Sirf RUNNING ko badalta hai, taaki finished run kabhi na palte."""
        if self.status == Status.RUNNING:
            self.status = Status.NOT_RESPONDING
            self.save(update_fields=["status", "updated_at"])
            return True
        return False


class ProcessLog(models.Model):
    """Ek log line - dashboard ke "Live logs" panel ke liye.

    Format daily log jaisa hi: stage + level + message.
    """

    LEVELS = [
        ("INFO", "Info"),
        ("WARN", "Warning"),
        ("ERROR", "Error"),
        ("DEBUG", "Debug"),
    ]

    run = models.ForeignKey(ProcessRun, on_delete=models.CASCADE, related_name="logs")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    stage = models.CharField(max_length=32, choices=Stage.choices, blank=True, default="")
    level = models.CharField(max_length=8, choices=LEVELS, default="INFO")
    message = models.TextField()

    class Meta:
        ordering = ["timestamp", "id"]
        indexes = [models.Index(fields=["run", "timestamp"])]

    def __str__(self):
        return f"[{self.stage}] {self.message[:60]}"
