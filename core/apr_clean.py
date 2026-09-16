"""Shared APR cleaning step, parameterised per process.

Takes the raw export a scraper left in Media/<Process>/APR_data/<Y>/<M>/<D>/
and writes the cleaned, headerless workbook the downstream system expects:

    Media/<Process>/APR_Clean/<Y>/<M>/<D>/<date>_APR.xlsx

The transformation is carried over unchanged from the original per-process
scripts. What varies between them is only which columns are subtracted from
Login Duration, and whether the process is split into a/b legs - both are
arguments rather than seven copies of the same 200 lines.

Output lands under the *source* process, not under the "<X> Clean" folder the
old scripts used, so there is one APR_Clean location per process and the
dashboard's APR Clean tab finds it.
"""

import os
import shutil
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import common
from core import daily_log
from core.errors import CentralErrorHandler
from core.runlog import Stage, start_run

#: Subtracted from Login Duration by most processes.
TOTAL_BREAK = ("Total Break Duration",)

#: Smart Dial exports lead with a row-number column that the cleaned file does
#: not want. Other dialers do not have it.
ROW_NUMBER_COLUMN = "##"


def time_to_minutes(value):
    """'08:56:45' -> 536.75. Anything unparseable counts as zero."""
    if isinstance(value, str):
        try:
            hours, minutes, seconds = map(int, value.split(":"))
            return hours * 60 + minutes + seconds / 60
        except ValueError:
            return 0
    return 0


def _map_agent_ids(ctx, column, mapping_env, tag=""):
    """Translate dialer agent ids into ATS employee codes."""
    relative = os.getenv(mapping_env, "")
    if not relative:
        ctx.warn(f"{tag}{mapping_env} is not set; agent ids left as the dialer "
                 "reports them")
        return column
    path = os.path.join(common.PROJECT_ROOT, relative)
    if not os.path.exists(path):
        ctx.warn(f"{tag}Mapping file not found: {ctx.relative(path)}; agent ids "
                 "left as the dialer reports them")
        return column

    table = pd.read_excel(path)
    missing = [c for c in ("Agent Id", "ATS ID") if c not in table.columns]
    if missing:
        ctx.warn(f"{tag}{os.path.basename(path)} is missing {', '.join(missing)}; "
                 "agent ids left unchanged")
        return column
    mapping = dict(zip(table["Agent Id"].astype(str).str.strip(),
                       table["ATS ID"].astype(str).str.strip()))

    # pandas reads the ids as floats, so "3367" arrives as "3367.0".
    cleaned = column.astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    mapped = cleaned.map(mapping).fillna(cleaned)
    unmapped = int((mapped == cleaned).sum())
    if unmapped:
        ctx.warn(f"{tag}{unmapped} agent id(s) had no ATS code in "
                 f"{os.path.basename(path)}; left unchanged")
    return mapped


def _upload(ctx, path, process):
    """SFTP the cleaned workbook, if the transfer is configured.

    Silently skipped when the settings are absent: a machine that only needs
    the local file should not fail the run over a missing upload target.
    """
    host = os.getenv("APR_SFTP_HOST", "")
    user = os.getenv("APR_SFTP_USERNAME", "")
    password = os.getenv("APR_SFTP_PASSWORD", "")
    if not (host and user and password):
        ctx.detail("APR_SFTP_* not configured; upload skipped")
        return

    base = os.getenv("APR_SFTP_REMOTE_BASE", "").rstrip("/")
    # The source process, not the cleaner's own name: the remote tree is
    # organised by the process the data belongs to, so "DMI Clean_a" would
    # point at a directory that does not exist.
    remote_dir = os.getenv("APR_SFTP_REMOTE_DIR") or (
        f"{base}/{process}" if base else "")
    if not remote_dir:
        ctx.warn("APR_SFTP_REMOTE_BASE/DIR not set; upload skipped")
        return

    import paramiko
    transport = None
    try:
        transport = paramiko.Transport((host, int(os.getenv("APR_SFTP_PORT", "22"))))
        transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            sftp.chdir(remote_dir)
        except IOError:
            ctx.warn(f"Remote directory does not exist: {remote_dir}; upload skipped")
            return
        sftp.put(path, f"{remote_dir}/{os.path.basename(path)}")
        ctx.step(f"Uploaded {os.path.basename(path)} to {host}",
                 stage=Stage.PROCESSING)
    except Exception as exc:
        # The cleaned file is on disk either way; a transfer problem should
        # not throw away a good run.
        ctx.warn(f"SFTP upload failed: {type(exc).__name__}: {exc}")
    finally:
        if transport is not None:
            transport.close()


def run_clean(script_file, source_process, *, break_columns=TOTAL_BREAK,
              leg="", target_date=None, upload=True,
              agent_column="Agent ID", login_column="First Login",
              logout_column=None, id_mapping_env=None):
    """Clean one process's APR export. Returns a process exit code."""
    started = time.time()
    if target_date is None:
        target_date = common.as_date(sys.argv[1]) if len(sys.argv) > 1 else \
            common.as_date(datetime.today() - timedelta(days=1))

    # The dashboard still reports this run under the cleaner's own name, but
    # the daily log belongs to the process whose export is being cleaned:
    # Media/<SourceProcess>/APR_Clean_logs/<Y>/<M>/<D>/. Keeping it beside the
    # cleaned file means one place to look per process, rather than a log tree
    # under every "<X> Clean" folder.
    ctx = start_run(script_file, target_date,
                    daily_processes=[source_process],
                    log_folder=common.FOLDER_APR_CLEAN_LOGS,
                    workflow_stage=daily_log.CLEANING)
    paths = common.ProcessPaths(source_process, target_date)
    handler = CentralErrorHandler(ctx)
    common.load_env()

    tag = f"[{leg}] " if leg else ""
    name = f"{paths.date_key}_{leg}{common.REPORT_TAG}" if leg else \
        f"{paths.date_key}_{common.REPORT_TAG}"
    source_path = os.path.join(paths.dataset(common.FOLDER_APR_RAW, create=False),
                               f"{name}.csv")
    target_dir = paths.dataset(common.FOLDER_APR_CLEAN)
    csv_path = os.path.join(target_dir, f"{name}.csv")
    xlsx_path = os.path.join(target_dir, f"{name}.xlsx")

    with ctx.stage_scope(Stage.PROCESSING):
        try:
            if not os.path.exists(source_path):
                ctx.fail(f"{tag}No export to clean: {ctx.relative(source_path)}. "
                         f"Run the {source_process} scraper for {paths.date_key} first.")
                ctx.failed()
                return 1
            shutil.copy2(source_path, csv_path)

            data = pd.read_csv(csv_path)
            required = ("Login Duration", agent_column, login_column) \
                + tuple(break_columns)
            absent = [c for c in required if c not in data.columns]
            if absent:
                ctx.fail(f"{tag}Export is missing column(s): {', '.join(absent)}; "
                         f"got {list(data.columns)[:10]}")
                os.remove(csv_path)
                ctx.failed()
                return 1

            minutes = data["Login Duration"].apply(time_to_minutes)
            data["Login Duration (minutes)"] = minutes
            for column in break_columns:
                converted = data[column].apply(time_to_minutes)
                data[f"{column} (minutes)"] = converted
                minutes = minutes - converted
            # OneXVoice reports Login Duration as 00:00:00 for a share of real
            # sessions even though it records both Login and Logout. Taking the
            # column at face value drops those agents, and the cleaned file then
            # disagrees with the scraper's own output for the same day.
            if logout_column and logout_column in data.columns:
                span = ((pd.to_datetime(data[logout_column], errors="coerce")
                         - pd.to_datetime(data[login_column], errors="coerce"))
                        .dt.total_seconds() / 60)
                recoverable = (minutes <= 0) & span.gt(0).fillna(False)
                if recoverable.any():
                    breaks = sum((data[c].apply(time_to_minutes) for c in break_columns),
                                 start=pd.Series(0, index=data.index))
                    minutes = minutes.mask(recoverable, span - breaks)
                    ctx.warn(f"{tag}{int(recoverable.sum())} session(s) had no "
                             "Login Duration; minutes taken from the Logout - "
                             "Login span instead")

            data["Minutes"] = minutes
            data = data[data["Minutes"] != 0.0]
            data[agent_column] = data[agent_column].astype(str).str.strip()

            # OneXVoice identifies agents by its own number; the downstream
            # consumer expects the ATS employee code, exactly as the scraper
            # produces. Without this the cleaned file carries ids nobody
            # downstream recognises.
            if id_mapping_env:
                data[agent_column] = _map_agent_ids(
                    ctx, data[agent_column], id_mapping_env, tag)

            for column in ("##", agent_column):
                if column in data.columns:
                    totals = data[data[column].astype(str)
                                  .str.contains("Total", case=False, na=False)].index
                    if len(totals):
                        # Label, not position: the two stop agreeing after the
                        # filters above.
                        data = data[data.index < totals[0]]

            data = data[~data.iloc[:, 0].astype(str)
                        .str.contains("ICAI", case=False, na=False)]

            data[login_column] = pd.to_datetime(data[login_column], errors="coerce") \
                                   .dt.strftime("%d-%b-%y")
            data["Minutes"] = np.ceil(data["Minutes"]).astype(int)
            ctx.step(f"{tag}Net Login added | {len(data)} agent(s)",
                     stage=Stage.PROCESSING)

            drop = ["Login Duration (minutes)", "Minutes"] + \
                   [f"{c} (minutes)" for c in break_columns]
            data.drop(columns=drop, inplace=True, errors="ignore")

            # The original dropped column 0 outright, which works only while
            # that column is Smart Dial's "##" row number. The OneXVoice export
            # behind DMI's leg a starts with Agent Id, so dropping blindly
            # threw the agent identifier away and left names in its place.
            # Drop the row-number column by name, or nothing.
            if ROW_NUMBER_COLUMN in data.columns:
                data.drop(columns=[ROW_NUMBER_COLUMN], inplace=True)
            else:
                ctx.detail(f"{tag}no {ROW_NUMBER_COLUMN!r} column; no column dropped")

            # Headerless, as the downstream system expects.
            data.to_excel(xlsx_path, index=False, header=False)
            os.remove(csv_path)
            ctx.step(f"{tag}XLSX created: {os.path.basename(xlsx_path)}",
                     stage=Stage.PROCESSING)
            ctx.set_records(scraped=len(data))

            if upload:
                _upload(ctx, xlsx_path, source_process)
        except Exception as exc:
            handler.handle(exc, stage=Stage.PROCESSING)
            for stale in (csv_path,):
                if os.path.exists(stale):
                    os.remove(stale)
            ctx.failed()
            return 1

    ctx.success(f"{tag}Completed in {time.time() - started:.2f}s")
    return 0
