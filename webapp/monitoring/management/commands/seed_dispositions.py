"""Register the Disposition Report scrapers in the roster.

These are not in dashboard.py's CATEGORIES, and the rows seed_processes does
create for them point at folders that never existed ("DMI_Disposition/
dialer_data"). They are listed here instead, by hand, because each one's
owning process is a business fact rather than something derivable from the
path: Scrapper/Smart_Dial/DMI_Disposition/script.py reports on DMI and writes
into Media/DMI, the same way Imagine_Disposition.py has always written into
Media/Imagine.

Idempotent - run it again after adding a process.

    python manage.py seed_dispositions
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from monitoring.models import Process, ProcessKind, ScheduleType

#: (roster name, script folder, script file, owning process).
#: The owning process decides the Media folder, so the disposition report sits
#: beside the APR data for the same client rather than in a folder named after
#: the script that fetched it.
DISPOSITIONS = [
    ("Imagine Disposition", "Scrapper/Smart_Dial/Imagine",
     "Imagine_Disposition.py", "Imagine"),
    ("DMI Disposition", "Scrapper/Smart_Dial/DMI_Disposition",
     "script.py", "DMI"),
    ("GOQII Disposition", "Scrapper/Smart_Dial/GOQII_Disposition",
     "script.py", "GOQII"),
    ("IRDAI Disposition", "Scrapper/Smart_Dial/IRDAI_Disposition",
     "script.py", "IRDAI"),
    # Leg B only: leg A's login offers no Disposition Report at all. See
    # Scrapper/Smart_Dial/TNCM_Disposition_a/script.py.
    ("TN CM Disposition", "Scrapper/Smart_Dial/TNCM_Disposition_b",
     "script.py", "TN CM"),
    # A separate tenant on OneXVoice rather than Smart Dial, so it keeps its
    # own folder instead of writing into Media/DMI.
    ("DMI vKYC Disposition", "Scrapper/Smart_Dial/DMI_Disposition_vkyc",
     "script.py", "DMI_vKYC"),
]

#: TN CM writes one file per leg, so its name carries the leg suffix.
FILE_PATTERNS = {"TN CM Disposition": "{date}_B.xlsx"}

#: Names dashboard.py's CATEGORIES still carries for these reports, which
#: seed_processes turns into rows pointing at folders that never existed
#: ("DMI_Disposition/dialer_data"). The entries above replace them, so leaving
#: these in place shows every disposition twice in the sidebar - once real and
#: once dead. seed_processes skips them by importing this list, so removing
#: them here is permanent rather than undone by the next roster seed.
SUPERSEDED = [
    "DMI_Disposition", "DMI_Disposition_vkyc", "GOQII_Disposition",
    "ICAI_Disposition", "Imagine_Disposition", "IRDAI_Disposition",
    "TN_CM_Disposition_a", "TN_CM_Disposition_b",
]


class Command(BaseCommand):
    help = "Seed Process rows for the Disposition Report scrapers"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        root = Path(settings.PROJECT_ROOT)
        created = updated = missing = 0

        for name, working_dir, script_name, owner in DISPOSITIONS:
            exists = (root / working_dir / script_name).is_file()
            missing += not exists
            defaults = {
                "category": "Smart_Dial",  # matches the existing roster group, not a new one
                "kind": ProcessKind.DISPOSITION,
                "working_dir": working_dir,
                "script_name": script_name,
                "output_dir": f"{owner}/Disposition_data",
                "file_pattern": FILE_PATTERNS.get(name, "{date}.xlsx"),
                "range_aware": False,
                "schedule_type": ScheduleType.SCHEDULED,
                "is_active": exists,
            }
            if options["dry_run"]:
                self.stdout.write(f"  would upsert {name:24} -> "
                                  f"Media/{owner}/Disposition_data "
                                  f"active={exists}")
                continue
            _, was_created = Process.objects.update_or_create(
                name=name, defaults=defaults)
            created += was_created
            updated += not was_created

        # Deleted, not deactivated: an inactive row still shows in the
        # sidebar, so deactivating left every disposition listed twice. Any
        # row that somehow has run history is kept and only deactivated, so
        # no record is ever destroyed to tidy up the list.
        removed = kept = 0
        for process in Process.objects.filter(name__in=SUPERSEDED):
            if process.runs.exists():
                if not options["dry_run"] and process.is_active:
                    process.is_active = False
                    process.save(update_fields=["is_active"])
                kept += 1
                continue
            if options["dry_run"]:
                self.stdout.write(f"  would delete superseded {process.name}")
            else:
                process.delete()
            removed += 1

        self.stdout.write(self.style.SUCCESS(
            f"dispositions: {len(DISPOSITIONS)} | created {created} | "
            f"updated {updated} | script missing {missing} | "
            f"superseded rows deleted {removed}"
            + (f" | kept (has run history) {kept}" if kept else "")
        ))
