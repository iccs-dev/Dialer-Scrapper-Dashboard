"""Register combine.py and hrms.py as roster entries.

They are not scrapers, but they do report to the monitoring API - combine.py
as process "combine", hrms.py as "hrms", both from common.detect_process().
Without a row to resolve against, every callback they make is answered with
404 and dropped, which is why HRMS activity never appeared on the dashboard.

    python manage.py seed_workflow
"""

from django.core.management.base import BaseCommand

from monitoring.models import Process, ProcessKind, ScheduleType

#: name -> field values. The names must match common.detect_process(), which
#: derives them from the script filename for anything at the project root.
WORKFLOW_STEPS = {
    "combine": {
        "category": "Workflow",
        "script_name": "combine.py",
        # combine.py writes the HRMS upload file; that file appearing is what
        # proves it did its job.
        "output_dir": "hrms/upload",
        "file_pattern": "{date}.csv",
    },
    "hrms": {
        "category": "Workflow",
        "script_name": "hrms.py",
        # Uploads to a remote system and leaves nothing on disk, so there is
        # no output file to verify - the exit code is the only signal.
        "output_dir": "",
        "file_pattern": "",
    },
}


class Command(BaseCommand):
    help = "Register combine.py and hrms.py so their monitoring callbacks resolve"

    def handle(self, *args, **options):
        for name, fields in WORKFLOW_STEPS.items():
            process, created = Process.objects.update_or_create(
                name=name,
                defaults={
                    # Both live at the project root, so working_dir is empty.
                    "working_dir": "",
                    "kind": ProcessKind.WORKFLOW,
                    "schedule_type": ScheduleType.SCHEDULED,
                    "range_aware": False,
                    "is_active": True,
                    **fields,
                },
            )
            verb = "created" if created else "updated"
            self.stdout.write(f"  {verb}: {name} -> {process.script_path}")
        self.stdout.write(self.style.SUCCESS("workflow steps registered"))
