import streamlit as st
import subprocess
import threading
import time
import os
import pandas as pd
from datetime import datetime, timedelta

# ==================== CONFIG ====================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_EXE = os.path.join(SCRIPT_DIR, "venv", "Scripts", "python.exe")

CATEGORIES = {
    "Smart_Dial": {
        "path": os.path.join(SCRIPT_DIR, "Scrapper", "Smart_Dial"),
        "processes": ["ICAI",
                      "ICAI Clean", 
                      "ICAI_Disposition",
                      "Imagine", 
                      "Imagine Clean",
                      "Imagine_Disposition",
                    #   "NHDC", 
                      "TN CM", 
                      "TN CM_Clean",
                      "TN_CM_Disposition_a",
                     "TN_CM_Disposition_b",
                      "Amazon Merchant",
                      "Amazon Merchant Clean",
                      "BharatPe", 
                      "IRDAI",
                      "IRDAI Clean",
                      "IRDAI_Disposition",  
                    #   "KPN", 
                      "GOQII",
                      "GOQII Clean",
                      "GOQII_Disposition",
                      "DMI",
                      "DMI Clean_b", 
                      "DMI_Disposition",
                      "DMI_Disposition_vkyc"
                      
                    #   "Rlife",
                    #   "HR Calling",
                    #   "KPN1", 
                    #   "KPN2", 
                    #   "KPN3"
                    ],
    },
    "Sampark": {
        "path": os.path.join(SCRIPT_DIR, "Scrapper", "Sampark"),
        "processes": ["MMT Holiday",
                      "MMT Holiday Clean"],
    },
    "Xtend_Call_Center": {
        "path": os.path.join(SCRIPT_DIR, "Scrapper", "Xtend_Call_Center"),
        "processes": ["Muthoot","Muthoot Clean", 
                      "Muthoot_PL","Muthoot_PL_Clean"
                      ],
    },
    "Convox": {
        "path": os.path.join(SCRIPT_DIR, "Scrapper", "Convox"),
        "processes": [
                    #   "L&T SME", 
                      "Edelweiss",
                     
                      ],
    },
    "Techinfo": {
        "path": os.path.join(SCRIPT_DIR, "Scrapper", "Techinfo"),
        "processes": [
            "Bajaj Capital",
            "Qurex", 
            "JIO Insurance", 
            "Unicef",
            "D2H_Dish"
            ],
    },
    "Client": {
        "path": os.path.join(SCRIPT_DIR, "Scrapper", "Client"),
        "processes": ["NHA","NHA Clean"],
    },
    # NHA report scrapers exposed as individual UI processes. Each runs a
    # sibling script inside Scrapper/Client/NHA/ and writes to its own
    # subdirectory under media/NHA/. All are range-aware (one login per range).
    "NHA_Reports": {
        "path": os.path.join(SCRIPT_DIR, "Scrapper", "Client"),
        "processes": [
            {"name": "NHA_CDR",       "folder": "NHA", "script": "combineCDR.py",       "output_dir": "NHA/combineCDR_data",       "file_pattern": "{date}_CDR.csv",       "range_aware": True},
            {"name": "NHA_IBTAG",     "folder": "NHA", "script": "ibtag.py",     "output_dir": "NHA/IB_tag_data",    "file_pattern": "{date}_IBTAG.csv",     "range_aware": True},
            {"name": "NHA_INTERVAL",  "folder": "NHA", "script": "interval.py",  "output_dir": "NHA/Interval_data",  "file_pattern": "{date}_INTERVAL.csv",  "range_aware": True},
            {"name": "NHA_IVR",       "folder": "NHA", "script": "ivr.py",       "output_dir": "NHA/IVR_data",       "file_pattern": "{date}_IVR.xlsx",      "range_aware": True},
            {"name": "NHA_OBTAG",     "folder": "NHA", "script": "obtag.py",     "output_dir": "NHA/OB_tag_data",    "file_pattern": "{date}_OBTAG.xlsx",    "range_aware": True},
            {"name": "NHA_QUEUES",    "folder": "NHA", "script": "queues.py",    "output_dir": "NHA/Queue_data",     "file_pattern": "{date}_QUEUE.csv",     "range_aware": True},
            {"name": "NHA_TAG_ABDM",  "folder": "NHA", "script": "tag_abdm.py",  "output_dir": "NHA/Tag_data_abdm",  "file_pattern": "{date}_tag_abdm.csv",  "range_aware": True},
            {"name": "NHA_TAG_COVID", "folder": "NHA", "script": "tag_covid.py", "output_dir": "NHA/Tag_data_covid", "file_pattern": "{date}_tag_covid.csv", "range_aware": True},
            {"name": "NHA_TAG_COVIN", "folder": "NHA", "script": "tag_covin.py", "output_dir": "NHA/Tag_data_covin", "file_pattern": "{date}_tag_covin.csv", "range_aware": True},
            {"name": "NHA_TAG_PMJAY", "folder": "NHA", "script": "tag_pmjay.py", "output_dir": "NHA/Tag_data_pmjay", "file_pattern": "{date}_tag_pmjay.csv", "range_aware": True},
        ],
    },
}


def _normalize_process(entry, category_path):
    """Normalize a CATEGORIES process entry into a metadata dict.

    Backward-compatible: a plain string entry (legacy form) maps to
    folder=name, script="script.py", output_dir="{name}/dialer_data",
    file_pattern="{date}_APR.csv", range_aware=False — i.e. the existing
    convention. A dict entry overrides any of those fields explicitly.
    """
    if isinstance(entry, str):
        name = entry
        folder = entry
        script = "script.py"
        output_dir = f"{entry}/dialer_data"
        file_pattern = "{date}_APR.csv"
        range_aware = False
    else:
        name = entry["name"]
        folder = entry.get("folder", name)
        script = entry.get("script", "script.py")
        output_dir = entry.get("output_dir", f"{name}/dialer_data")
        file_pattern = entry.get("file_pattern", "{date}_APR.csv")
        range_aware = entry.get("range_aware", False)
    return {
        "name": name,
        "cwd": os.path.join(category_path, folder),
        "script": script,
        "output_dir": output_dir,        # relative to SCRIPT_DIR/media
        "file_pattern": file_pattern,    # python format string with {date}
        "range_aware": range_aware,
    }


# Build name → metadata map and per-category name list for the UI.
PROCESS_META = {}
CATEGORY_NAMES = {}
ALL_PROCESSES = []
for cat_name, cat_info in CATEGORIES.items():
    names = []
    for entry in cat_info["processes"]:
        meta = _normalize_process(entry, cat_info["path"])
        PROCESS_META[meta["name"]] = meta 
        ALL_PROCESSES.append(meta["name"])
        names.append(meta["name"])
    CATEGORY_NAMES[cat_name] = names

# Backward-compat alias — code paths that only need cwd keep working.
PROCESS_PATH_MAP = {name: meta["cwd"] for name, meta in PROCESS_META.items()}

# ==================== THREAD-SAFE GLOBALS ====================
# Use dict/list containers so the background thread's references survive reruns
DASHBOARD_LOG_DIR = os.path.join(SCRIPT_DIR, "LOGs", "Dashboard")
os.makedirs(DASHBOARD_LOG_DIR, exist_ok=True)

# Guard: only initialize these once, not on every Streamlit rerun
if "_dashboard_state" not in globals():
    _dashboard_state = {
        "run_status": "idle",
        "process_status": {},
        "log_file_path": None,
    }


def add_log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_line = f"[{timestamp}] {msg}"
    log_path = _dashboard_state["log_file_path"]
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception:
            pass


def flush_logs():
    # Read log file path from session_state (set when run starts)
    log_path = st.session_state.get("log_file_path") or _dashboard_state.get("log_file_path")
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                st.session_state.log_lines = f.read().splitlines()
        except Exception:
            pass
    # Sync process status and run status from thread-safe globals
    st.session_state.process_status = dict(_dashboard_state["process_status"])
    st.session_state.status = _dashboard_state["run_status"]


# ==================== SESSION STATE ====================
if "status" not in st.session_state:
    st.session_state.status = "idle"
if "log_lines" not in st.session_state:
    st.session_state.log_lines = []
if "thread" not in st.session_state:
    st.session_state.thread = None
if "process_status" not in st.session_state:
    st.session_state.process_status = {}
if "log_file_path" not in st.session_state:
    st.session_state.log_file_path = None
if "select_all" not in st.session_state:
    st.session_state.select_all = True


# ==================== WORKER FUNCTIONS ====================
def run_single_script(process_name, date_args=None):
    """Run one process's script with the given date CLI args.

    date_args is a list of strings appended to the script invocation:
      []                    → script default (yesterday)
      ["2026-04-08"]        → single date
      ["2026-04-01", "2026-04-08"] → inclusive range (range-aware procs only)

    Success is verified by checking that file_pattern resolves to an
    existing file for every date covered by date_args.
    """
    if date_args is None:
        date_args = []

    meta = PROCESS_META[process_name]
    script_path = os.path.join(meta["cwd"], meta["script"])

    if not os.path.exists(script_path):
        add_log(f"  SKIP {process_name} - {meta['script']} not found")
        _dashboard_state["process_status"][process_name] = "failed"
        return False

    cmd = [PYTHON_EXE, script_path] + list(date_args)

    _dashboard_state["process_status"][process_name] = "running"
    add_log(f"  START {process_name}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=meta["cwd"],
        )

        output_lines = []
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                output_lines.append(line)
                add_log(f"    {process_name}: {line}")

        proc.wait()
        
        
        # ================= Run disposition.py after ICAI =================
        # if proc.returncode == 0 and process_name == "ICAI":

        #     disposition_script = os.path.join(meta["cwd"], "disposition.py")

        #     if os.path.exists(disposition_script):

        #         add_log("------------------------------------------------")
        #         add_log("STARTING ICAI DISPOSITION")
        #         add_log("------------------------------------------------")

        #         disp_proc = subprocess.Popen(
        #             [PYTHON_EXE, disposition_script] + list(date_args),
        #             stdout=subprocess.PIPE,
        #             stderr=subprocess.STDOUT,
        #             text=True,
        #             cwd=meta["cwd"]
        #         )

        #         for line in disp_proc.stdout:
        #             line = line.strip()
        #             if line:
        #                 add_log(f"DISPOSITION : {line}")

        #         disp_proc.wait()

        #         if disp_proc.returncode == 0:
        #             add_log("ICAI DISPOSITION COMPLETED SUCCESSFULLY")
        #         else:
        #             add_log(f"ICAI DISPOSITION FAILED (Exit Code {disp_proc.returncode})")

        #         add_log("------------------------------------------------")

        #     else:
        #         add_log("disposition.py NOT FOUND")
        if proc.returncode == 0:
            media_dir = os.path.join(SCRIPT_DIR, "media", meta["output_dir"])
            check_dates = _expected_dates_from_args(date_args)
            missing = []
            sizes = []
            for d in check_dates:
                fp = os.path.join(media_dir, meta["file_pattern"].format(date=d))
                if os.path.exists(fp):
                    sizes.append(os.path.getsize(fp))
                else:
                    missing.append(d)

            if not missing:
                total = sum(sizes)
                if len(check_dates) == 1:
                    add_log(f"  OK {process_name} - Data saved ({total:,} bytes)")
                else:
                    add_log(f"  OK {process_name} - {len(check_dates)} files saved ({total:,} bytes total)")
                _dashboard_state["process_status"][process_name] = "success"
            else:
                add_log(f"  WARN {process_name} - Script ran but missing file(s) for: {', '.join(missing)}")
                _dashboard_state["process_status"][process_name] = "warning"
            return True
        else:
            error_lines = output_lines[-3:] if output_lines else ["No output"]
            for el in error_lines:
                add_log(f"  ERR {process_name} - {el}")
            _dashboard_state["process_status"][process_name] = "failed"
            return False

    except Exception as e:
        add_log(f"  FAIL {process_name} - {str(e)}")
        _dashboard_state["process_status"][process_name] = "failed"
        return False


def _expected_dates_from_args(date_args):
    """Expand date_args into the inclusive list of dates to verify on disk."""
    if not date_args:
        return [(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")]
    if len(date_args) == 1:
        return [date_args[0]]
    start = datetime.strptime(date_args[0], "%Y-%m-%d").date()
    end = datetime.strptime(date_args[1], "%Y-%m-%d").date()
    out = []
    cur = start
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def run_combine(date_str=None):
    add_log("--- COMBINE + HRMS ---")
    cmd = [PYTHON_EXE, os.path.join(SCRIPT_DIR, "combine.py")]
    if date_str:
        cmd.append(date_str)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=SCRIPT_DIR,
        )

        for line in proc.stdout:
            line = line.rstrip()
            if line:
                add_log(f"  COMBINE: {line}")

        proc.wait()

        if date_str:
            upload_file = os.path.join(SCRIPT_DIR, "media", "Upload", f"{date_str}.csv")
        else:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            upload_file = os.path.join(SCRIPT_DIR, "media", "Upload", f"{yesterday}.csv")

        if os.path.exists(upload_file):
            file_size = os.path.getsize(upload_file)
            add_log(f"  OK Combined file saved ({file_size:,} bytes)")
        else:
            add_log(f"  WARN No combined upload file found")

        # Check HRMS status
        combine_log_dir = os.path.join(SCRIPT_DIR, "LOGs", "Combine_error")
        today_log = os.path.join(combine_log_dir, f"{datetime.now().strftime('%d-%m-%Y')}_combine_log.txt")
        if os.path.exists(today_log):
            with open(today_log, "r") as f:
                content = f.read()
                if "Executed hrms.py" in content:
                    add_log(f"  OK HRMS upload triggered")
                elif "HRMS upload skipped" in content:
                    add_log(f"  INFO HRMS upload skipped (disabled)")

        if proc.returncode == 0:
            add_log(f"  OK combine.py completed successfully")
            return True
        else:
            add_log(f"  FAIL combine.py exited with code {proc.returncode}")
            return False

    except Exception as e:
        add_log(f"  FAIL combine.py - {str(e)}")
        return False


def run_scraper(dates, processes, log_file_path):
    _dashboard_state["run_status"] = "running"
    _dashboard_state["process_status"].clear()
    _dashboard_state["log_file_path"] = log_file_path

    total_success = 0
    total_failed = 0

    # Split range-aware processes off when there's a real multi-date window
    # so each runs once with [start, end] (single login per range) instead of
    # logging in once per date in the outer loop.
    real_dates = [d for d in dates if d]
    if len(real_dates) > 1:
        range_aware_procs = [p for p in processes if PROCESS_META[p]["range_aware"]]
        per_date_procs    = [p for p in processes if not PROCESS_META[p]["range_aware"]]
    else:
        range_aware_procs = []
        per_date_procs = list(processes)

    try:
        # Mark every selected process pending up front so the UI shows them
        # all from the start, even before their first execution slot.
        for p in processes:
            _dashboard_state["process_status"][p] = "pending"

        if range_aware_procs:
            add_log(f"========== RANGE: {real_dates[0]} → {real_dates[-1]} (range-aware) ==========")
            for process_name in range_aware_procs:
                success = run_single_script(process_name, [real_dates[0], real_dates[-1]])
                if success:
                    total_success += 1
                else:
                    total_failed += 1
            add_log("")

        for date_str in dates:
            display_date = date_str if date_str else "yesterday"
            add_log(f"========== DATE: {display_date} ==========")

            # Reset only per-date procs each iteration; range-aware ones have
            # already finished and their final status must not be overwritten.
            for p in per_date_procs:
                _dashboard_state["process_status"][p] = "pending"

            if per_date_procs:
                add_log("--- SCRAPING ---")
                for process_name in per_date_procs:
                    date_args = [date_str] if date_str else []
                    success = run_single_script(process_name, date_args)
                    if success:
                        total_success += 1
                    else:
                        total_failed += 1

            run_combine(date_str if date_str else None)
            add_log("")

        add_log(f"========== SUMMARY ==========")
        add_log(f"  Total processes: {total_success + total_failed}")
        add_log(f"  Successful: {total_success}")
        add_log(f"  Failed: {total_failed}")
        add_log(f"  Log saved: {log_file_path}")
        add_log(f"========== DONE ==========")
        _dashboard_state["run_status"] = "complete" if total_failed == 0 else "failed"

    except Exception as e:
        add_log(f"FATAL ERROR: {str(e)}")
        _dashboard_state["run_status"] = "failed"


# ==================== UI ====================
st.set_page_config(page_title="Dialer Scraper Dashboard", layout="wide")
st.title("Dialer Scraper Dashboard")

# ---------- Sidebar ----------
with st.sidebar:
    st.header("Configuration")

    date_mode = st.radio("Date Mode", ["Yesterday (Default)", "Single Date", "Date Range"])

    target_dates = []
    if date_mode == "Yesterday (Default)":
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        st.info(f"Will scrape for: {yesterday}")
        target_dates = [""]
    elif date_mode == "Single Date":
        single_date = st.date_input("Select Date", value=datetime.now() - timedelta(days=1))
        target_dates = [single_date.strftime("%Y-%m-%d")]
        st.info(f"Will scrape for: {target_dates[0]}")
    else:
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start Date", value=datetime.now() - timedelta(days=7))
        with col2:
            end_date = st.date_input("End Date", value=datetime.now() - timedelta(days=1))
        if start_date > end_date:
            st.error("Start date must be before end date!")
            target_dates = []
        else:
            current = start_date
            while current <= end_date:
                target_dates.append(current.strftime("%Y-%m-%d"))
                current += timedelta(days=1)
            st.info(f"Will scrape {len(target_dates)} days: {target_dates[0]} to {target_dates[-1]}")

    st.divider()

    # Process selection
    st.subheader("Processes")

    def toggle_select_all():
        new_val = not st.session_state.select_all
        st.session_state.select_all = new_val
        for p in ALL_PROCESSES:
            st.session_state[f"proc_{p}"] = new_val

    st.checkbox(
        "Select All / Deselect All",
        value=st.session_state.select_all,
        on_change=toggle_select_all,
        key="select_all_checkbox"
    )

    selected_processes = []
    for cat_name, names in CATEGORY_NAMES.items():
        with st.expander(f"{cat_name} ({len(names)})", expanded=True):
            for p in names:
                if f"proc_{p}" not in st.session_state:
                    st.session_state[f"proc_{p}"] = st.session_state.select_all
                checked = st.checkbox(p, key=f"proc_{p}")
                if checked:
                    selected_processes.append(p)

    st.divider()

    # Run button
    is_running = _dashboard_state["run_status"] == "running"
    run_disabled = is_running or len(selected_processes) == 0 or len(target_dates) == 0

    if st.button("Run Scraper", type="primary", disabled=run_disabled, use_container_width=True):
        st.session_state.log_lines = []
        st.session_state.process_status = {}
        # Generate log file path and save to session state BEFORE starting thread
        log_timestamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
        log_path = os.path.join(DASHBOARD_LOG_DIR, f"{log_timestamp}_dashboard.log")
        st.session_state.log_file_path = log_path
        thread = threading.Thread(
            target=run_scraper,
            args=(target_dates, selected_processes, log_path),
            daemon=True,
        )
        thread.start()
        st.session_state.thread = thread
        st.rerun()

# ---------- Main Area (Tabs) ----------
tab_scraper, tab_monitor = st.tabs(["Scraper", "Monitoring"])

# ==================== SCRAPER TAB ====================
with tab_scraper:
    # Flush logs from background thread
    flush_logs()

    # Status indicator
    status = st.session_state.status
    if status == "idle":
        st.info("Ready. Configure settings and click Run.")
    elif status == "running":
        st.warning("Scraping in progress... (auto-refreshing every 2s)")
    elif status == "complete":
        st.success("All tasks completed successfully!")
    elif status == "failed":
        st.error("Some tasks failed! Check logs below.")

    # Process status table
    if st.session_state.process_status:
        st.subheader("Process Status")
        cols = st.columns(5)
        for i, (proc, stat) in enumerate(st.session_state.process_status.items()):
            col = cols[i % 5]
            if stat == "success":
                col.success(f"{proc}")
            elif stat == "failed":
                col.error(f"{proc}")
            elif stat == "running":
                col.warning(f"{proc}...")
            elif stat == "warning":
                col.warning(f"{proc} (no data)")
            else:
                col.caption(f"{proc} (pending)")

        col1, col2, col3, col4 = st.columns(4)
        success_count = sum(1 for s in st.session_state.process_status.values() if s == "success")
        failed_count = sum(1 for s in st.session_state.process_status.values() if s == "failed")
        warning_count = sum(1 for s in st.session_state.process_status.values() if s == "warning")
        pending_count = sum(1 for s in st.session_state.process_status.values() if s in ("pending", "running"))
        with col1:
            st.metric("Success", success_count)
        with col2:
            st.metric("Failed", failed_count)
        with col3:
            st.metric("Warnings", warning_count)
        with col4:
            st.metric("Pending", pending_count)

    # Live logs
    st.subheader("Logs")
    log_container = st.container(height=500)

    with log_container:
        if st.session_state.log_lines:
            log_text = "\n".join(st.session_state.log_lines)
            st.code(log_text, language="log")
        else:
            st.caption("No logs yet. Click Run to start.")

    # Auto-refresh while running
    if _dashboard_state["run_status"] == "running":
        time.sleep(2)
        st.rerun()

    # Reset button
    if status in ["complete", "failed"]:
        if st.button("Clear & Reset"):
            st.session_state.status = "idle"
            st.session_state.log_lines = []
            st.session_state.log_file_path = None
            st.session_state.process_status = {}
            _dashboard_state["run_status"] = "idle"
            _dashboard_state["process_status"].clear()
            _dashboard_state["log_file_path"] = None
            st.rerun()


# ==================== MONITORING TAB ====================
MONITOR_PROCESSES = ["ICAI",
                     "ICAI Clean", 
                     
                     "Imagine", 
                     "Imagine Clean",
                    #  "Imagine_Disposition",
                    #  "NHDC", 
                     "TN CM",
                     "TN CM_Clean",
                    #  "TN CM_Disposition_a",
                    #  "TN CM_Disposition_b",
                     "TN CM_Clean",
                     "Amazon Merchant",
                     "Amazon Merchant Clean", 
                     "NHA", 
                     "NHA Clean",
                     "Muthoot",
                     "Muthoot Clean",
                     "Muthoot_PL",
                     "Muthoot_PL_Clean",
                     "IRDAI",
                     "IRDAI Clean", 
                    #  "IRDAI_Disposition",
                    #  "KPN", 
                     "GOQII",
                     "GOQII Clean", 
                    #  "GOQII_Disposition", 
                     "DMI", 
                    
                     "DMI Clean_b",
                    #  "DMI_Disposition",
                    #  "DMI_Disposition_vkyc"
                    #  "Rlife", 
                     "MMT Holiday",
                     "MMT Holiday Clean",
                    #  "L&T SME", 
                     "Edelweiss",
                 
                     "Bajaj Capital",
                    #  "Qurex", 
                    #  "JIO Insurance", 
                    #  "Unicef",
                    #  "BharatPe", 
                    #  "HR Calling", 
                    #  "KPN1", 
                    #  "KPN2",
                    #  "KPN3",
                    #  "D2H_Dish",
                    #  "NHA_CDR", 
                    #  "NHA_IBTAG",
                    #  "NHA_INTERVAL", 
                    #  "NHA_IVR", 
                    #  "NHA_OBTAG",
                    #  "NHA_QUEUES", 
                    #  "NHA_TAG_ABDM", 
                    #  "NHA_TAG_COVID", 
                    #  "NHA_TAG_COVIN", 
                    #  "NHA_TAG_PMJAY"
                     ]


def get_scrape_row_count(process_name, date_str):
    """Check if scraped data exists for a process+date, return row count or 0.

    Paths come from PROCESS_META so per-process output dirs and file
    patterns (e.g. NHA_*) resolve correctly. Falls back to the legacy APR
    layout for any process not present in PROCESS_META.
    """
    if process_name in PROCESS_META:
        meta = PROCESS_META[process_name]
        media_dir = os.path.join(SCRIPT_DIR, "media", meta["output_dir"])
        primary = os.path.join(media_dir, meta["file_pattern"].format(date=date_str))
        # If pattern is a CSV, also try the XLSX sibling and vice-versa —
        # mirrors the original APR fallback for legacy processes.
        siblings = [primary]
        if primary.endswith(".csv"):
            siblings.append(primary[:-4] + ".xlsx")
        elif primary.endswith(".xlsx"):
            siblings.append(primary[:-5] + ".csv")
        candidates = siblings
    else:
        candidates = [
            os.path.join(SCRIPT_DIR, "media", process_name, "dialer_data", f"{date_str}_APR.csv"),
            os.path.join(SCRIPT_DIR, "media", process_name, "dialer_data", f"{date_str}_APR.xlsx"),
        ]

    for path in candidates:
        if os.path.exists(path):
            try:
                if path.endswith(".csv"):
                    df = pd.read_csv(path)
                else:
                    df = pd.read_excel(path)
                return len(df)
            except Exception:
                return -1  # file exists but unreadable
    return 0


def get_upload_info(date_str):
    """Check combined upload file. Return dict of {process: row_count} and total rows."""
    upload_path = os.path.join(SCRIPT_DIR, "media", "Upload", f"{date_str}.csv")
    if not os.path.exists(upload_path):
        return None, 0
    try:
        df = pd.read_csv(upload_path)
        total = len(df)
        if "Process" in df.columns:
            counts = df["Process"].value_counts().to_dict()
        else:
            counts = {}
        return counts, total
    except Exception:
        return None, 0


def get_already_processed(date_str):
    """Processes whose agents HRMS refused because the day was already closed.

    Those agents are absent from the upload file, but not because anything went
    wrong: combine.py strips rejected EmpCodes and retries, and HRMS rejects a
    day whose attendance HR has already processed. Nothing is outstanding for
    them, so they count as done rather than missing.

    Returns {process: rejected_count} for that reason only.
    """
    path = os.path.join(SCRIPT_DIR, "media", "Failed_Agent_list", f"{date_str}.csv")
    if not os.path.exists(path):
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
        process = str(row.get("Process", "")).strip()
        if process and process.lower() not in ("nan", "unknown"):
            counts[process] = counts.get(process, 0) + 1
    return counts


def get_hrms_status(date_str):
    """Check if HRMS upload was successful for a given data date."""
    # Check new daily log dir first
    hrms_log_dir = os.path.join(SCRIPT_DIR, "LOGs", "HRMS")
    # HRMS logs are named by run date, not data date, so scan all recent logs
    # and look for the data date in the log content
    if os.path.exists(hrms_log_dir):
        for log_name in sorted(os.listdir(hrms_log_dir), reverse=True):
            log_path = os.path.join(hrms_log_dir, log_name)
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    if date_str in content and "Success message detected" in content:
                        return True
            except Exception:
                continue

    # Fallback: check old hrms_log.log
    old_log = os.path.join(SCRIPT_DIR, "hrms_log.log")
    if os.path.exists(old_log):
        try:
            with open(old_log, "r", encoding="utf-8") as f:
                # Read last 500 lines to avoid loading huge file
                lines = f.readlines()[-500:]
                content = "".join(lines)
                if date_str in content and "Success message detected" in content:
                    return True
        except Exception:
            pass

    return False


def get_hrms_process_detail(date_str):
    """Get per-process HRMS upload details for a data date.
    Returns dict: {process: {"uploaded": N, "failed": N}} or None if no data."""
    upload_path = os.path.join(SCRIPT_DIR, "media", "Upload", f"{date_str}.csv")
    failed_path = os.path.join(SCRIPT_DIR, "media", "Failed_Agent_list", f"{date_str}.csv")

    if not os.path.exists(upload_path):
        return None

    result = {}
    try:
        upload_df = pd.read_csv(upload_path)
        if "Process" in upload_df.columns:
            for proc, count in upload_df["Process"].value_counts().items():
                result[proc] = {"uploaded": count, "failed": 0}
    except Exception:
        return None

    if os.path.exists(failed_path):
        try:
            failed_df = pd.read_csv(failed_path)
            if "Process" in failed_df.columns:
                for proc, count in failed_df["Process"].value_counts().items():
                    if proc not in result:
                        result[proc] = {"uploaded": 0, "failed": 0}
                    result[proc]["failed"] = count
        except Exception:
            pass

    return result


with tab_monitor:
    # Date range selector
    mon_col1, mon_col2, mon_col3 = st.columns([1, 1, 2])
    with mon_col1:
        mon_start = st.date_input("From", value=datetime.now() - timedelta(days=7), key="mon_start")
    with mon_col2:
        mon_end = st.date_input("To", value=datetime.now() - timedelta(days=1), key="mon_end")
    with mon_col3:
        st.markdown("")
        st.markdown(
            '<span style="display:inline-block;width:14px;height:14px;background:#d4edda;border:1px solid #b1dfbb;border-radius:3px;vertical-align:middle"></span> Scraped + Uploaded&nbsp;&nbsp;'
            '<span style="display:inline-block;width:14px;height:14px;background:#fff3cd;border:1px solid #ffc107;border-radius:3px;vertical-align:middle"></span> Scraped only&nbsp;&nbsp;'
            '<span style="display:inline-block;width:14px;height:14px;background:#f8d7da;border:1px solid #f5c6cb;border-radius:3px;vertical-align:middle"></span> Missing',
            unsafe_allow_html=True,
        )

    if mon_start > mon_end:
        st.error("Start date must be before end date!")
    else:
        # Build date list
        mon_dates = []
        current_d = mon_start
        while current_d <= mon_end:
            mon_dates.append(current_d.strftime("%Y-%m-%d"))
            current_d += timedelta(days=1)

        # Collect data
        matrix_data = []
        totals_scraped = 0
        totals_uploaded = 0
        totals_missing = 0

        for date_str in reversed(mon_dates):
            row = {"Date": date_str}
            for proc in MONITOR_PROCESSES:
                row[proc] = get_scrape_row_count(proc, date_str)
            upload_counts, upload_total = get_upload_info(date_str)
            row["_upload_counts"] = upload_counts
            row["_upload_total"] = upload_total
            row["_hrms"] = get_hrms_status(date_str)
            row["_already"] = get_already_processed(date_str)
            matrix_data.append(row)

        # ── Summary metrics ──
        total_cells = 0
        green_cells = 0
        yellow_cells = 0
        red_cells = 0
        for row in matrix_data:
            uc = row["_upload_counts"] or {}
            ap = row.get("_already") or {}
            for proc in MONITOR_PROCESSES:
                total_cells += 1
                sc = row[proc]
                iu = uc.get(proc, 0)
                # Rejected as "attendance already processed" counts as done:
                # HRMS closed the day, so there is nothing left to upload.
                if sc > 0 and (iu > 0 or ap.get(proc, 0) > 0):
                    green_cells += 1
                elif sc > 0:
                    yellow_cells += 1
                else:
                    red_cells += 1

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Dates", len(mon_dates))
        m2.metric("Scraped + Uploaded", green_cells)
        m3.metric("Scraped Only", yellow_cells)
        m4.metric("Missing", red_cells)

        # ── Process Scrape Status Table ──
        st.markdown("##### Scrape Status by Process")

        display_data = []
        for row in matrix_data:
            upload_counts = row["_upload_counts"] or {}
            already = row.get("_already") or {}
            display_row = {"Date": row["Date"]}
            for proc in MONITOR_PROCESSES:
                sc = row[proc]
                iu = upload_counts.get(proc, 0)
                # Rejected as "attendance already processed" reads the same as a
                # normal upload - HRMS closed the day, so nothing is outstanding.
                if sc > 0 and (iu > 0 or already.get(proc, 0) > 0):
                    display_row[proc] = f"{sc}"
                elif sc > 0:
                    display_row[proc] = f"{sc}*"
                else:
                    display_row[proc] = ""
            display_data.append(display_row)

        df_display = pd.DataFrame(display_data)

        def style_scrape_cell(val):
            if val == "":
                return "background-color: #f8d7da; color: #721c24; text-align: center"
            elif isinstance(val, str) and val.endswith("*"):
                return "background-color: #fff3cd; color: #856404; font-weight: bold; text-align: center"
            else:
                return "background-color: #d4edda; color: #155724; font-weight: bold; text-align: center"

        def style_date(val):
            return "font-weight: bold; background-color: #2c3e50; color: #ffffff; text-align: center"

        proc_cols = [c for c in df_display.columns if c != "Date"]
        styled_df = (
            df_display.style
            .map(style_scrape_cell, subset=proc_cols)
            .map(style_date, subset=["Date"])
            .set_properties(**{"text-align": "center", "font-size": "13px"})
            .set_table_styles([
                {"selector": "th", "props": [
                    ("background-color", "#1f77b4"),
                    ("color", "white"),
                    ("font-size", "12px"),
                    ("text-align", "center"),
                    ("padding", "6px 4px"),
                    ("white-space", "nowrap"),
                ]},
                {"selector": "td", "props": [
                    ("padding", "5px 4px"),
                    ("min-width", "50px"),
                ]},
            ])
        )

        st.dataframe(styled_df, use_container_width=True, hide_index=True, height=min(400, 60 + len(mon_dates) * 38))
        st.caption("`N` = rows scraped & uploaded | `N*` = scraped but NOT in combined upload | Empty red = missing data")

        # ── Upload & HRMS Status Table ──
        st.markdown("##### Combined Upload & HRMS Status")

        upload_data = []
        for row in matrix_data:
            upload_counts = row["_upload_counts"] or {}
            upload_total = row["_upload_total"]
            hrms = row["_hrms"]

            # Count processes in upload
            procs_in_upload = len(upload_counts) if upload_counts else 0
            total_procs_scraped = sum(1 for p in MONITOR_PROCESSES if row[p] > 0)

            upload_data.append({
                "Date": row["Date"],
                "Processes Scraped": total_procs_scraped,
                "In Upload File": procs_in_upload,
                "Upload Rows": upload_total if upload_total > 0 else "-",
                "HRMS": "Pushed" if hrms else "Not pushed",
            })

        df_upload = pd.DataFrame(upload_data)

        def style_upload_rows(val):
            if val == "-":
                return "background-color: #f8d7da; color: #721c24; text-align: center"
            return "text-align: center"

        def style_hrms(val):
            if val == "Pushed":
                return "background-color: #d4edda; color: #155724; font-weight: bold; text-align: center"
            return "background-color: #f8d7da; color: #721c24; text-align: center"

        def style_procs(val, col_name, row_data):
            return "text-align: center"

        styled_upload = (
            df_upload.style
            .map(style_date, subset=["Date"])
            .map(style_upload_rows, subset=["Upload Rows"])
            .map(style_hrms, subset=["HRMS"])
            .set_properties(**{"text-align": "center", "font-size": "13px"})
            .set_table_styles([
                {"selector": "th", "props": [
                    ("background-color", "#1f77b4"),
                    ("color", "white"),
                    ("font-size", "13px"),
                    ("text-align", "center"),
                    ("padding", "8px"),
                ]},
                {"selector": "td", "props": [
                    ("padding", "6px 8px"),
                ]},
            ])
        )

        st.dataframe(styled_upload, use_container_width=True, hide_index=True)

        # ── HRMS Push Status by Process ──
        st.markdown("##### HRMS Push Status by Process")

        hrms_matrix_data = []
        hrms_processes_seen = set()
        for row in matrix_data:
            detail = get_hrms_process_detail(row["Date"])
            if detail:
                hrms_processes_seen.update(detail.keys())
            hrms_matrix_data.append({"Date": row["Date"], "_detail": detail, "_hrms": row["_hrms"]})

        hrms_procs = sorted(hrms_processes_seen) if hrms_processes_seen else MONITOR_PROCESSES

        hrms_display = []
        for row in hrms_matrix_data:
            detail = row["_detail"]
            d_row = {"Date": row["Date"]}
            for proc in hrms_procs:
                if detail and proc in detail:
                    uploaded = detail[proc]["uploaded"]
                    failed = detail[proc]["failed"]
                    if failed > 0:
                        d_row[proc] = f"{uploaded} ({failed}F)"
                    else:
                        d_row[proc] = str(uploaded)
                elif detail is not None:
                    d_row[proc] = ""
                else:
                    d_row[proc] = "-"
            d_row["Status"] = "Pushed" if row["_hrms"] else "Not pushed"
            hrms_display.append(d_row)

        df_hrms = pd.DataFrame(hrms_display)

        def style_hrms_cell(val):
            if val == "" or val == "-":
                return "background-color: #f8d7da; color: #721c24; text-align: center"
            elif "F)" in str(val):
                return "background-color: #fff3cd; color: #856404; font-weight: bold; text-align: center"
            else:
                return "background-color: #d4edda; color: #155724; font-weight: bold; text-align: center"

        hrms_proc_cols = [c for c in df_hrms.columns if c not in ("Date", "Status")]
        styled_hrms = (
            df_hrms.style
            .map(style_hrms_cell, subset=hrms_proc_cols)
            .map(style_date, subset=["Date"])
            .map(style_hrms, subset=["Status"])
            .set_properties(**{"text-align": "center", "font-size": "13px"})
            .set_table_styles([
                {"selector": "th", "props": [
                    ("background-color", "#1f77b4"),
                    ("color", "white"),
                    ("font-size", "12px"),
                    ("text-align", "center"),
                    ("padding", "6px 4px"),
                    ("white-space", "nowrap"),
                ]},
                {"selector": "td", "props": [
                    ("padding", "5px 4px"),
                    ("min-width", "50px"),
                ]},
            ])
        )

        st.dataframe(styled_hrms, use_container_width=True, hide_index=True, height=min(400, 60 + len(mon_dates) * 38))
        st.caption("`N` = agents uploaded | `N (MF)` = N uploaded, M failed | `-` = no upload file | Empty red = process not in upload")
