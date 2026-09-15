"""Import the process roster from the existing Streamlit dashboard.py.

The roster is read from dashboard.py's CATEGORIES rather than retyped, so the
dashboard you already use stays the source of truth. CATEGORIES contains
os.path.join(...) calls, so it cannot be literal-eval'd - the module is
exec'd with a stub `streamlit` so importing it does not need Streamlit
installed or a browser session.

Processes whose script is missing on this machine are kept in the list but
marked is_active=False, so the UI stays familiar and they light up as each one
is ported.
"""

import sys
import types
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from monitoring.models import Process, ScheduleType


class Command(BaseCommand):
    help = "Seed Process rows from the Streamlit dashboard.py roster"

    def add_arguments(self, parser):
        parser.add_argument("--source", default=None,
                            help="Path to dashboard.py (default: project root)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        source = Path(options["source"] or (settings.PROJECT_ROOT / "dashboard.py"))
        if not source.is_file() or source.stat().st_size == 0:
            self.stderr.write(f"dashboard.py not found or empty: {source}")
            return

        meta = self._load_process_meta(source)
        if not meta:
            self.stderr.write("No processes found in CATEGORIES")
            return

        root = Path(settings.PROJECT_ROOT)
        created = updated = inactive = 0
        for name, info in meta.items():
            working_dir = self._relative(info["cwd"], root)
            script = root / working_dir / info["script"] if working_dir else None
            exists = bool(script and script.is_file())

            defaults = {
                "category": info["category"],
                "working_dir": working_dir,
                "script_name": info["script"],
                "output_dir": info["output_dir"],
                "file_pattern": info["file_pattern"],
                "range_aware": info["range_aware"],
                "schedule_type": ScheduleType.SCHEDULED,
                "is_active": exists,
            }
            if not exists:
                inactive += 1
            if options["dry_run"]:
                self.stdout.write(f"  would upsert {name:28} active={exists}")
                continue
            _, was_created = Process.objects.update_or_create(name=name, defaults=defaults)
            created += was_created
            updated += not was_created

        self.stdout.write(self.style.SUCCESS(
            f"roster: {len(meta)} processes | created {created} | updated {updated} "
            f"| inactive (script missing) {inactive}"
        ))

    @staticmethod
    def _relative(path, root):
        try:
            return str(Path(path).resolve().relative_to(root.resolve()))
        except (ValueError, OSError):
            return ""

    def _load_process_meta(self, source):
        """Exec dashboard.py far enough to read CATEGORIES and _normalize_process."""
        stub = types.ModuleType("streamlit")

        def _noop(*args, **kwargs):
            return None

        class _Any:
            def __getattr__(self, item):
                return _noop

            def __call__(self, *a, **k):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        stub.__getattr__ = lambda name: _Any()
        saved = sys.modules.get("streamlit")
        sys.modules["streamlit"] = stub

        namespace = {"__file__": str(source), "__name__": "dashboard_roster"}
        text = source.read_text(encoding="utf-8")
        # Only the config half is needed; stop before the UI starts touching
        # Streamlit state, which a stub cannot honestly emulate.
        cut = text.find("# ==================== THREAD-SAFE GLOBALS")
        if cut == -1:
            cut = len(text)
        try:
            exec(compile(text[:cut], str(source), "exec"), namespace)
        finally:
            if saved is not None:
                sys.modules["streamlit"] = saved
            else:
                sys.modules.pop("streamlit", None)

        categories = namespace.get("CATEGORIES") or {}
        normalise = namespace.get("_normalize_process")
        meta = {}
        for category, info in categories.items():
            for entry in info.get("processes", []):
                item = normalise(entry, info["path"])
                item["category"] = category
                meta[item["name"]] = item
        return meta
