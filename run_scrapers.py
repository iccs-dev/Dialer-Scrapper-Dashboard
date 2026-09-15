"""Run every process scraper for one date, isolating failures.

Each scraper runs in its own subprocess, so a crash, a hang or a hard exit in
one process cannot stop the others: the orchestrator records the outcome and
moves on. Each scraper already emails its own alert through the central error
handler, so this layer only has to sequence them and summarise.

    venv/bin/python run_scrapers.py                 # yesterday, all processes
    venv/bin/python run_scrapers.py 2026-09-06
    venv/bin/python run_scrapers.py --only Imagine
    venv/bin/python run_scrapers.py --timeout 900
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
from core.runlog import Stage, start_run

#: Jobs are discovered, not listed. script.py is a process's main scraper;
#: *_Disposition.py is an additional report for the same process.
SCRAPER_GLOBS = ("Scrapper/*/*/script.py", "Scrapper/*/*/*_Disposition.py")

#: Per-process ceiling. A hung dialer must not block the rest of the night.
DEFAULT_TIMEOUT_SECONDS = 1800


def discover(only=None):
    """[(label, path)] for every scraper job in the project.

    The label is the process for its main scraper, and "<process>/<report>"
    for an extra report, so two jobs of the same process stay distinguishable
    in the summary.
    """
    root = Path(common.CODE_ROOT)
    found = {}
    for pattern in SCRAPER_GLOBS:
        for path in sorted(root.glob(pattern)):
            process = common.detect_process(str(path))
            stem = path.stem
            label = process if stem.lower() == "script" else f"{process}/{stem}"
            if only and not {process.lower(), label.lower()} & {n.lower() for n in only}:
                continue
            found[str(path)] = (label, path)
    return [found[k] for k in sorted(found)]


def run_one(label, script, date_key, timeout):
    """Run one scraper. Never raises - the outcome is the return value."""
    started = datetime.now()
    try:
        completed = subprocess.run(
            [sys.executable, str(script), date_key],
            cwd=common.CODE_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        status = "success" if completed.returncode == 0 else "failed"
        detail = ""
        if status == "failed":
            tail = [line for line in (completed.stdout or "").splitlines() if line.strip()]
            detail = tail[-1][:300] if tail else (completed.stderr or "")[-300:]
        return {
            "process": label, "status": status, "returncode": completed.returncode,
            "detail": detail, "seconds": (datetime.now() - started).total_seconds(),
        }
    except subprocess.TimeoutExpired:
        return {
            "process": label, "status": "timeout", "returncode": None,
            "detail": f"No output within {timeout}s; process killed",
            "seconds": (datetime.now() - started).total_seconds(),
        }
    except Exception as exc:
        # Could not even start it: still not a reason to stop the others.
        return {
            "process": label, "status": "error", "returncode": None,
            "detail": f"{type(exc).__name__}: {exc}",
            "seconds": (datetime.now() - started).total_seconds(),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", help="report date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--only", nargs="+", metavar="PROCESS",
                        help="run only these processes")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help=f"per-process timeout in seconds (default {DEFAULT_TIMEOUT_SECONDS})")
    parser.add_argument("--list", action="store_true", help="list scrapers and exit")
    args = parser.parse_args()

    target = common.as_date(args.date) if args.date else \
        common.as_date(datetime.today() - timedelta(days=1))
    date_key = target.strftime(common.DATE_FORMAT)

    scrapers = discover(args.only)
    if args.list:
        for label, path in scrapers:
            print(f"  {label:22} {path.relative_to(common.CODE_ROOT)}")
        return 0
    if not scrapers:
        print(f"No scrapers matched {SCRAPER_GLOBS}"
              + (f" for {args.only}" if args.only else ""))
        return 1

    ctx = start_run(__file__, target)
    ctx.info(f"Running {len(scrapers)} job(s) for {date_key}: "
             + ", ".join(label for label, _ in scrapers))

    results = []
    for label, script in scrapers:
        ctx.info(f"--- {label} ---", stage=Stage.SCRAPING)
        outcome = run_one(label, script, date_key, args.timeout)
        results.append(outcome)
        line = (f"{label}: {outcome['status']} in {outcome['seconds']:.0f}s"
                + (f" | {outcome['detail']}" if outcome["detail"] else ""))
        if outcome["status"] == "success":
            ctx.info(line, stage=Stage.SCRAPING)
        else:
            # Logged, not raised: the next process still runs.
            ctx.error(line, stage=Stage.SCRAPING)

    succeeded = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]

    print(f"\n{'Job':22} {'Status':10} {'Seconds':>8}")
    print("-" * 46)
    for row in results:
        print(f"{row['process']:22} {row['status']:10} {row['seconds']:>8.0f}")
    print(f"\n{len(succeeded)}/{len(results)} succeeded"
          + (f"; failed: {', '.join(r['process'] for r in failed)}" if failed else ""))

    ctx.info(f"{len(succeeded)}/{len(results)} scraper(s) succeeded")
    ctx.finish(status="completed" if not failed else "completed with failures")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
