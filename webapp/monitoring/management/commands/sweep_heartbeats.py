"""Flip RUNNING runs with no recent heartbeat to NOT_RESPONDING.

Run from a systemd timer (Stage 8). The dashboard also sweeps on every state
request, so the screen is correct even between timer ticks - this command
exists so the database is correct even when nobody is looking at it.
"""

from django.core.management.base import BaseCommand

from monitoring.api import sweep_stale_runs


class Command(BaseCommand):
    help = "Mark stale RUNNING runs as NOT_RESPONDING"

    def handle(self, *args, **options):
        changed = sweep_stale_runs()
        self.stdout.write(self.style.SUCCESS(f"marked {changed} run(s) NOT_RESPONDING"))
