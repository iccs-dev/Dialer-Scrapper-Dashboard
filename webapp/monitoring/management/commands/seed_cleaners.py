"""Register the APR Clean processes in the roster.

This exists because `kind` used to be hand-set. seed_processes builds the
roster from dashboard.py's CATEGORIES and never sets `kind`, so every cleaner
it creates lands as the model default, SCRAPER. On the machine this was
developed on someone fixed that with a one-off Django shell command, which
lives in one SQLite file and in no migration, fixture or script - so a fresh
server got a roster where the cleaners were SCRAPER rows. That put them in the
Scrapper + HRMS tab, left the APR Clean Status tab filtering on a kind no row
had, and showed "None are available" in its Process filter.

The same applies to output_dir and file_pattern: dashboard.py describes these
as ordinary scrapers writing "<name>/dialer_data", but a cleaner writes
"<source process>/APR_Clean" - Imagine Clean's output belongs under
Media/Imagine, beside the APR data it was cleaned from. Those corrections were
hand-made too, and any seed_processes run reverted them.

Both facts now live here. seed_processes skips these names (it imports
MANAGED), so re-running it can no longer undo them.

Idempotent - run it again after adding a process.

    python manage.py seed_cleaners
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from monitoring.models import Process, ProcessKind, ScheduleType

#: (roster name, script folder, source process, file pattern).
#:
#: The source process decides the Media folder: a cleaner reads that process's
#: export and writes back beside it, so "Imagine Clean" reports on
#: Media/Imagine/APR_Clean rather than a folder of its own.
#:
#: The file pattern is what the cleaner actually writes, confirmed against
#: disk. DMI and TN CM are dialled on two legs and write one workbook each
#: ("<date>_aAPR.xlsx", "<date>_bAPR.xlsx"). DMI has a roster row per leg so
#: each is reported separately; TN CM has a single row, so its pattern is a
#: wildcard and media_reader sums the legs - without it the tab reported only
#: leg a and undercounted TN CM by the whole of leg b.
CLEANERS = [
    ("Imagine Clean",         "Scrapper/Smart_Dial/Imagine Clean",
     "Imagine",         "{date}_APR.xlsx"),
    ("GOQII Clean",           "Scrapper/Smart_Dial/GOQII Clean",
     "GOQII",           "{date}_APR.xlsx"),
    ("IRDAI Clean",           "Scrapper/Smart_Dial/IRDAI Clean",
     "IRDAI",           "{date}_APR.xlsx"),
    ("Amazon Merchant Clean", "Scrapper/Smart_Dial/Amazon Merchant Clean",
     "Amazon Merchant", "{date}_APR.xlsx"),
    ("DMI Clean_a",           "Scrapper/Smart_Dial/DMI Clean_a",
     "DMI",             "{date}_aAPR.xlsx"),
    ("DMI Clean_b",           "Scrapper/Smart_Dial/DMI Clean_b",
     "DMI",             "{date}_bAPR.xlsx"),
    ("TN CM_Clean",           "Scrapper/Smart_Dial/TN CM_Clean",
     "TN CM",           "{date}_*APR.xlsx"),
    # Edelweiss: leg a = Hyderabad, leg b = Chennai, one workbook each.
    ("Edelweiss Clean_a",     "Scrapper/Convox/Edelweiss Clean_a",
     "Edelweiss",       "{date}_aAPR.xlsx"),
    ("Edelweiss Clean_b",     "Scrapper/Convox/Edelweiss Clean_b",
     "Edelweiss",       "{date}_bAPR.xlsx"),
]

#: Names seed_processes must leave alone, because this command owns their
#: kind, output_dir and file_pattern. Imported there rather than duplicated.
MANAGED = [name for name, _folder, _source, _pattern in CLEANERS]

#: Roster names that no longer exist. update_or_create never removes a row, so
#: without this a renamed cleaner lingers in the dashboard's process list.
#: "Edelweiss Clean" (one combined workbook) briefly replaced Clean_a/_b on
#: 2026-11-24 and was withdrawn the same day.
RETIRED = ["Edelweiss Clean"]


def _category(working_dir):
    """Scrapper/<Category>/<Process> -> <Category>, e.g. "Convox"."""
    parts = Path(working_dir).parts
    return parts[1] if len(parts) > 2 and parts[0] == "Scrapper" else "Smart_Dial"


class Command(BaseCommand):
    help = "Seed Process rows for the APR Clean processes"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        root = Path(settings.PROJECT_ROOT)
        created = updated = missing = 0

        for name, working_dir, source, pattern in CLEANERS:
            exists = (root / working_dir / "script.py").is_file()
            missing += not exists
            defaults = {
                "category": _category(working_dir),
                "kind": ProcessKind.CLEANER,
                "working_dir": working_dir,
                "script_name": "script.py",
                "output_dir": f"{source}/APR_Clean",
                "file_pattern": pattern,
                "range_aware": False,
                "schedule_type": ScheduleType.SCHEDULED,
                "is_active": exists,
            }
            if options["dry_run"]:
                self.stdout.write(
                    f"  would upsert {name:24} -> Media/{source}/APR_Clean/"
                    f"{pattern}  active={exists}")
                continue
            _, was_created = Process.objects.update_or_create(
                name=name, defaults=defaults)
            created += was_created
            updated += not was_created

        removed = 0
        for process in Process.objects.filter(name__in=RETIRED):
            runs = process.runs.count()
            if options["dry_run"]:
                self.stdout.write(f"  would remove {process.name} ({runs} run(s) of history)")
                continue
            process.delete()   # cascades to its ProcessRun / ProcessLog rows
            removed += 1
            self.stdout.write(f"  removed retired {process.name} ({runs} run(s) of history)")

        self.stdout.write(self.style.SUCCESS(
            f"cleaners: {len(CLEANERS)} | created {created} | "
            f"updated {updated} | removed {removed} | script missing {missing}"
        ))
