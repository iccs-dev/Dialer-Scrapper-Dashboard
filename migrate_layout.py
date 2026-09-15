"""Move existing media files into the process- and date-partitioned layout.

Target layout, identical for data and logs::

    Media/<process>/<folder>/<YYYY>/<MM>/<DD>/<file>

    Media/Imagine/APR_data/2026/09/07/2026-09-07_APR.csv
    Media/Imagine/logs/2026/09/07/script.log

This tool handles three legacy shapes:

1. date-named files sitting flat in a data folder
   (``Media/Imagine/APR_data/2026-09-07_APR.xls``);
2. logs in a shared top-level folder (``Media/log/2026/09/07/script.log``),
   routed to the process that owns each script;
3. data folders at the media root rather than under a process
   (``Media/upload``), moved when ``--legacy-process`` names their owner.

Nothing here is hardcoded: process names and folder names come from
``common``, and dates come from the file names or the folders they sit in.

    venv/bin/python migrate_layout.py --dry-run
    venv/bin/python migrate_layout.py
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common


#: Folder names earlier versions used for the shared log tree at the media
#: root. Migration tools have to know the names they are migrating away from;
#: nothing outside this module refers to them.
LEGACY_LOG_FOLDERS = (common.FOLDER_LOGS, "log", "LOGs")

#: Log file names earlier versions used, mapped to the name the owning script
#: produces today. Files are renamed as they move.
LEGACY_LOG_ALIASES = {
    "scrapper.log": "script.log",
    "clean_apr.log": "cleaning_script.log",
}

_DATASET_NAMES = {name.lower() for name in common.DATASET_FOLDERS}
_DATASET_NAMES |= {name.lower() for name in LEGACY_LOG_FOLDERS}

#: Per-run logs written before the script name was part of the file name.
UNNAMED_RUN_LOG = re.compile(r"^(?P<stamp>\d{8}_\d{6})_(?P<run>[0-9a-f]{6,})\.log$")
#: The startup banner every run writes: "... | Run started | script=<name> ..."
BANNER_SCRIPT = re.compile(r"script=(?P<script>[\w.\-]+)")

DATE_IN_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
DATE_IN_PATH = re.compile(r"(\d{4})[/\\](\d{2})[/\\](\d{2})$")

#: The raw report folder must end up holding .csv, not the dialer's .xls.
CONVERT_TO_CSV = {common.FOLDER_APR_RAW.lower()}

SKIP_DIRS = {"venv", ".git", "__pycache__", "node_modules"}


def script_process_map():
    """``{'script.log': 'Imagine', 'hrms.log': 'hrms', ...}``.

    Built by asking ``common`` which process and log name each script in the
    project resolves to, so it stays correct if scripts move.
    """
    mapping = {}
    for dirpath, dirnames, filenames in os.walk(common.CODE_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                full = os.path.join(dirpath, name)
                mapping[common.script_log_name(full)] = common.detect_process(full)
    return mapping


def is_dataset_folder(name):
    return name.lower() in _DATASET_NAMES


def process_folders():
    """Top-level folders under the media root that represent a process."""
    return common.list_processes(exclude=LEGACY_LOG_FOLDERS)


def convert_xls_to_csv(source, destination):
    import pandas as pd

    pd.read_html(source)[0].to_csv(destination, index=False)


class Migrator:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.moved = self.converted = self.skipped = 0

    def _place(self, source, process, folder, date, target_name, convert=False):
        paths = common.ProcessPaths(process, date)
        target_dir = paths.dataset(folder, create=not self.dry_run)
        destination = os.path.join(target_dir, target_name)
        label = (
            f"{os.path.relpath(source, common.MEDIA_DIR)} -> "
            f"{os.path.relpath(destination, common.MEDIA_DIR)}"
        )

        if os.path.abspath(source) == os.path.abspath(destination):
            return
        if os.path.exists(destination):
            print(f"  SKIP (target exists): {label}")
            self.skipped += 1
            return

        action = "CONVERT" if convert else "MOVE"
        if not self.dry_run:
            if convert:
                convert_xls_to_csv(source, destination)
                os.remove(source)
            else:
                os.replace(source, destination)
        print(f"  {action}: {label}")
        self.moved += 1
        if convert:
            self.converted += 1

    def flatten_dataset(self, process, folder_path, folder_name):
        """Move date-named files sitting flat in a data folder into YYYY/MM/DD."""
        for name in sorted(os.listdir(folder_path)):
            source = os.path.join(folder_path, name)
            if not os.path.isfile(source):
                continue

            match = DATE_IN_NAME.match(name)
            if not match:
                print(f"  SKIP (no date in name): {os.path.relpath(source, common.MEDIA_DIR)}")
                self.skipped += 1
                continue

            convert = folder_name.lower() in CONVERT_TO_CSV and name.lower().endswith(".xls")
            target_name = name[: -len(".xls")] + ".csv" if convert else name
            self._place(source, process, folder_name, "-".join(match.groups()),
                        target_name, convert=convert)

    def migrate_processes(self):
        for process in process_folders():
            process_dir = os.path.join(common.MEDIA_DIR, process)
            for folder_name in sorted(os.listdir(process_dir)):
                folder_path = os.path.join(process_dir, folder_name)
                if os.path.isdir(folder_path):
                    self.flatten_dataset(process, folder_path, folder_name)

    def rename_unnamed_logs(self):
        """Add the script name to per-run logs that were written without it.

        Several scripts share a process folder, so '20260908_092403_225b87e1.log'
        does not say whether it came from script.py or Cleaning_Script.py. The
        script is read from the run's own startup banner rather than guessed.
        """
        for process in common.list_processes(exclude=LEGACY_LOG_FOLDERS):
            logs_root = common.media_path(process, common.FOLDER_LOGS)
            if not os.path.isdir(logs_root):
                continue
            for dirpath, _dirnames, filenames in os.walk(logs_root):
                for name in sorted(filenames):
                    match = UNNAMED_RUN_LOG.match(name)
                    if not match:
                        continue
                    source = os.path.join(dirpath, name)
                    script = self._script_from_banner(source)
                    if not script:
                        print(f"  SKIP (no script banner): "
                              f"{os.path.relpath(source, common.MEDIA_DIR)}")
                        self.skipped += 1
                        continue
                    target_name = (f"{script}_{match.group('stamp')}_"
                                   f"{match.group('run')}.log")
                    destination = os.path.join(dirpath, target_name)
                    label = (f"{os.path.relpath(source, common.MEDIA_DIR)} -> "
                             f"{target_name}")
                    if os.path.exists(destination):
                        print(f"  SKIP (target exists): {label}")
                        self.skipped += 1
                        continue
                    if not self.dry_run:
                        os.replace(source, destination)
                    print(f"  RENAME: {label}")
                    self.moved += 1

    @staticmethod
    def _script_from_banner(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for _ in range(5):
                    line = handle.readline()
                    if not line:
                        break
                    found = BANNER_SCRIPT.search(line)
                    if found:
                        return os.path.splitext(found.group("script"))[0].lower()
        except OSError:
            pass
        return None

    def migrate_legacy_logs(self):
        """Route a shared log tree at the media root to each owning process."""
        owners = script_process_map()

        for legacy_name in LEGACY_LOG_FOLDERS:
            legacy_root = common.media_path(legacy_name)
            if not os.path.isdir(legacy_root):
                continue
            # The target folder for this process is inside Media/<process>/,
            # never the legacy root itself.
            if os.path.abspath(legacy_root) == os.path.abspath(common.MEDIA_DIR):
                continue

            for dirpath, _dirnames, filenames in os.walk(legacy_root):
                date_match = DATE_IN_PATH.search(dirpath)
                for name in sorted(filenames):
                    source = os.path.join(dirpath, name)
                    target_name = LEGACY_LOG_ALIASES.get(name.lower(), name.lower())
                    process = owners.get(target_name)
                    if not process:
                        print(f"  SKIP (no script owns {name}): "
                              f"{os.path.relpath(source, common.MEDIA_DIR)}")
                        self.skipped += 1
                        continue

                    name_match = DATE_IN_NAME.match(name)
                    if date_match:
                        date = "-".join(date_match.groups())
                    elif name_match:
                        date = "-".join(name_match.groups())
                    else:
                        print(f"  SKIP (no date): "
                              f"{os.path.relpath(source, common.MEDIA_DIR)}")
                        self.skipped += 1
                        continue
                    self._place(source, process, common.FOLDER_LOGS, date, target_name)

    def migrate_legacy_datasets(self, process):
        """Move data folders that sit at the media root under *process*."""
        for name in sorted(os.listdir(common.MEDIA_DIR)):
            path = os.path.join(common.MEDIA_DIR, name)
            if not os.path.isdir(path) or not is_dataset_folder(name):
                continue
            if name.lower() in {n.lower() for n in LEGACY_LOG_FOLDERS}:
                continue  # handled by migrate_legacy_logs
            if not process:
                print(f"  SKIP (needs --legacy-process): {name}/")
                self.skipped += 1
                continue
            for dirpath, _dirnames, filenames in os.walk(path):
                date_match = DATE_IN_PATH.search(dirpath)
                for filename in sorted(filenames):
                    source = os.path.join(dirpath, filename)
                    name_match = DATE_IN_NAME.match(filename)
                    if date_match:
                        date = "-".join(date_match.groups())
                    elif name_match:
                        date = "-".join(name_match.groups())
                    else:
                        print(f"  SKIP (no date): {os.path.relpath(source, common.MEDIA_DIR)}")
                        self.skipped += 1
                        continue
                    self._place(source, process, name, date, filename)

    def prune_empty(self):
        """Remove the legacy folders this migration emptied.

        Scoped to the legacy roots on purpose: process folders ship with
        pre-created date directories that are meant to stay.
        """
        if self.dry_run:
            print("\n  (prune skipped in dry-run)")
            return

        roots = [common.media_path(name) for name in LEGACY_LOG_FOLDERS]
        roots += [
            os.path.join(common.MEDIA_DIR, name)
            for name in os.listdir(common.MEDIA_DIR)
            if os.path.isdir(os.path.join(common.MEDIA_DIR, name))
            and is_dataset_folder(name)
        ]

        for root in dict.fromkeys(os.path.abspath(r) for r in roots):
            if not os.path.isdir(root) or root == os.path.abspath(common.MEDIA_DIR):
                continue
            for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
                # Re-check on disk: the walk's snapshot goes stale as we delete.
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                        print(f"  RMDIR: {os.path.relpath(dirpath, common.MEDIA_DIR)}")
                except OSError:
                    pass

    def report(self):
        verb = "Would move" if self.dry_run else "Moved"
        print(f"\n{verb}: {self.moved}  (converted to csv: {self.converted})  "
              f"skipped: {self.skipped}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show what would happen")
    parser.add_argument(
        "--legacy-process",
        help="process to adopt data folders found at the media root (e.g. hrms)",
    )
    parser.add_argument(
        "--prune-empty",
        action="store_true",
        help="remove legacy folders left empty (never touches process folders)",
    )
    args = parser.parse_args()

    print(f"Media root: {common.MEDIA_DIR}")
    print(f"Processes : {', '.join(process_folders()) or '(none yet)'}\n")

    migrator = Migrator(dry_run=args.dry_run)
    migrator.migrate_processes()
    migrator.rename_unnamed_logs()
    migrator.migrate_legacy_logs()
    migrator.migrate_legacy_datasets(args.legacy_process)
    if args.prune_empty:
        migrator.prune_empty()
    migrator.report()


if __name__ == "__main__":
    main()
