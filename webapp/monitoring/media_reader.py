"""Reads the scraper's own output so the dashboard reports facts, not guesses.

This is a port of the Streamlit dashboard's data functions
(get_scrape_row_count / get_upload_info / get_already_processed /
get_hrms_status / get_hrms_process_detail) onto the dated layout the scrapers
write today.

The one substantive change is where files live:

    old (Streamlit)                          new (current scrapers)
    media/<output_dir>/<date>_APR.csv        Media/<output_dir>/<Y>/<M>/<D>/<date>_APR.csv
    media/Upload/<date>.csv                  Media/hrms/upload/<Y>/<M>/<D>/<date>.csv
    media/Failed_Agent_list/<date>.csv       Media/hrms/Failed_Agent_list/<Y>/<M>/<D>/<date>.csv
    LOGs/HRMS/*.log  (text scan)             Media/<Process>/Logs/<Y>/<M>/<D>/<Process>_<date>.log

The business rules are carried over unchanged - in particular the subtle one:
agents HRMS rejected as "attendance already processed" count as DONE, not
missing, because HRMS closed that day and nothing is outstanding.
"""

import sys
from pathlib import Path

from django.conf import settings

# The scraper package (common.py) lives one level above webapp/.
if str(settings.PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(settings.PROJECT_ROOT))

import common  # noqa: E402


def media_root():
    return Path(common.MEDIA_DIR)


def _date_parts(date_obj):
    return (f"{date_obj.year:04d}", f"{date_obj.month:02d}", f"{date_obj.day:02d}")


def _dated(*parts, date_obj):
    """Media/<parts...>/<YYYY>/<MM>/<DD>"""
    return media_root().joinpath(*parts, *_date_parts(date_obj))


def _row_count(path):
    """Rows in a CSV/XLSX, 0 if absent, -1 if present but unreadable."""
    import pandas as pd

    if not path.is_file():
        return None
    try:
        frame = pd.read_csv(path) if path.suffix == ".csv" else pd.read_excel(path)
        return len(frame)
    except Exception:
        return -1


def scrape_row_count(process, date_obj):
    """Rows this process produced for the date. 0 = missing, -1 = unreadable.

    `process` is a monitoring.models.Process; output_dir/file_pattern come
    straight from the roster so per-process layouts still resolve.
    """
    date_str = date_obj.isoformat()
    output_dir = process.output_dir or f"{process.name}/dialer_data"
    pattern = process.file_pattern or "{date}_APR.csv"
    folder = _dated(*output_dir.split("/"), date_obj=date_obj)

    primary = folder / pattern.format(date=date_str)
    # A CSV pattern may have an XLSX sibling and vice-versa, as before.
    candidates = [primary]
    if primary.suffix == ".csv":
        candidates.append(primary.with_suffix(".xlsx"))
    elif primary.suffix == ".xlsx":
        candidates.append(primary.with_suffix(".csv"))

    for path in candidates:
        count = _row_count(path)
        if count is not None:
            return count
    return 0


def _upload_path(date_obj):
    paths = common.ProcessPaths(common.UPLOAD_PROCESS, date_obj)
    return Path(paths.file(common.FOLDER_UPLOAD, paths.dated_name("csv"), create=False))


def _failed_path(date_obj):
    paths = common.ProcessPaths(common.UPLOAD_PROCESS, date_obj)
    return Path(
        paths.file(common.FOLDER_FAILED_AGENTS, paths.dated_name("csv"), create=False)
    )


def upload_info(date_obj):
    """({process: rows}, total) from the combined upload file."""
    import pandas as pd

    path = _upload_path(date_obj)
    if not path.is_file():
        return None, 0
    try:
        frame = pd.read_csv(path)
    except Exception:
        return None, 0
    counts = {}
    if "Process" in frame.columns:
        counts = {str(k): int(v) for k, v in frame["Process"].value_counts().items()}
    return counts, len(frame)


def already_processed(date_obj):
    """{process: count} of agents HRMS refused because the day was already closed.

    Those agents are absent from the upload file, but nothing is outstanding
    for them - so the dashboard must read this as done, not missing.
    """
    import pandas as pd

    path = _failed_path(date_obj)
    if not path.is_file():
        return {}
    try:
        frame = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
    except Exception:
        return {}
    if frame.empty:
        return {}

    # Two shapes on disk: headerless 4-column as hrms.py writes it, or
    # 6-column with a header once combine.py has rewritten it.
    if str(frame.iloc[0, 0]).strip().lower() == "error":
        frame.columns = [str(c).strip() for c in frame.iloc[0]]
        frame = frame.iloc[1:]
    else:
        names = ["Error", "EmpCode", "Date", "Minutes", "IsWH", "Process"]
        frame.columns = names[: frame.shape[1]]

    if "Process" not in frame.columns:
        return {}

    counts = {}
    for _, row in frame.iterrows():
        if "already processed" not in str(row.get("Error", "")).lower():
            continue
        name = str(row.get("Process", "")).strip()
        if name and name.lower() not in ("nan", "unknown"):
            counts[name] = counts.get(name, 0) + 1
    return counts


def hrms_process_detail(date_obj):
    """{process: {uploaded, failed}} or None when there is no upload file."""
    import pandas as pd

    upload = _upload_path(date_obj)
    if not upload.is_file():
        return None

    result = {}
    try:
        frame = pd.read_csv(upload)
        if "Process" in frame.columns:
            for name, count in frame["Process"].value_counts().items():
                result[str(name)] = {"uploaded": int(count), "failed": 0}
    except Exception:
        return None

    failed = _failed_path(date_obj)
    if failed.is_file():
        try:
            frame = pd.read_csv(failed)
            if "Process" in frame.columns:
                for name, count in frame["Process"].value_counts().items():
                    entry = result.setdefault(str(name), {"uploaded": 0, "failed": 0})
                    entry["failed"] = int(count)
        except Exception:
            pass
    return result


def daily_log_statuses(process_name, date_obj, folder=None):
    """{'SCRAPER': 'SUCCESS', 'COMBINE': ..., 'HRMS': ...} from the daily log.

    Replaces the old text scan of LOGs/HRMS: the combined daily log carries an
    explicit status block, so this is read rather than inferred.
    """
    import re

    paths = common.ProcessPaths(process_name, date_obj)
    path = (
        Path(paths.dataset(folder or common.FOLDER_APR_LOGS, create=False))
        / f"{process_name}_{date_obj.isoformat()}.log"
    )
    if not path.is_file():
        # Files written before APR and Disposition logs were split.
        path = (
            Path(paths.dataset(common.FOLDER_LOGS, create=False))
            / f"{process_name}_{date_obj.isoformat()}.log"
        )
    statuses = {"SCRAPER": "NOT RUN", "COMBINE": "NOT RUN", "HRMS": "NOT RUN"}
    if not path.is_file():
        return statuses, None
    text = path.read_text(encoding="utf-8", errors="replace")
    head = text.split("DETAILS", 1)[0]
    for line in head.splitlines():
        found = re.match(r"^(SCRAPER|COMBINE|HRMS)\s*:\s*(.+?)\s*$", line)
        if found:
            statuses[found.group(1)] = found.group(2)
    return statuses, str(path)


def hrms_status(date_obj, process_names):
    """True when any process's daily log says HRMS finished successfully."""
    for name in process_names:
        statuses, _ = daily_log_statuses(name, date_obj)
        if statuses.get("HRMS") == "SUCCESS":
            return True
    return False


#: The per-process dataset each extra status tab reports on. Both are folders
#: beside dialer_data under Media/<Process>/, in the same dated layout.
DATASET_FOLDERS = {
    "apr_clean": common.FOLDER_APR_CLEAN,
    "disposition": common.FOLDER_DISPOSITION,
}

#: Which workflow's daily log says whether a dataset reached HRMS. APR Clean
#: belongs to the APR workflow, which uploads; Disposition is its own workflow
#: and has no HRMS stage at all, so its rows are never reported as pushed.
DATASET_LOG_FOLDERS = {
    "apr_clean": common.FOLDER_APR_LOGS,
    "disposition": common.FOLDER_DISPOSITION_LOGS,
}


def dataset_row_count(process_name, date_obj, folder):
    """Rows in <process>/<folder>/Y/M/D for the date. 0 = nothing, -1 = unreadable.

    The file name differs per dataset - "<date>_APR.xlsx" for APR_Clean,
    "<date>.xlsx" for disposition - and a cleaned sibling may sit alongside, so
    the dated folder is scanned rather than a single name being guessed.
    """
    paths = common.ProcessPaths(process_name, date_obj)
    directory = Path(paths.dataset(folder, create=False))
    if not directory.is_dir():
        return 0

    date_str = date_obj.isoformat()
    candidates = sorted(
        entry for entry in directory.iterdir()
        if entry.is_file()
        and not entry.name.startswith(".")        # .download_* is a part file
        and entry.name.startswith(date_str)
        and entry.suffix.lower() in (".csv", ".xlsx", ".xls")
    )
    for path in candidates:
        count = _row_count(path)
        if count is not None:
            return count
    return 0
