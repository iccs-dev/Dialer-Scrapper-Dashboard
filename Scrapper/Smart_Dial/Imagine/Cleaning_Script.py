import os
import shutil
from datetime import datetime, timedelta
import subprocess
import pandas as pd
import numpy as np
from openpyxl import load_workbook
import paramiko  # <-- For SFTP upload
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import common
from core.errors import CentralErrorHandler
from core.runlog import Stage, start_run

common.load_env()

if len(sys.argv) > 1:
    _target_date = common.as_date(sys.argv[1])
else:
    _target_date = common.as_date(datetime.today() - timedelta(days=1))
# ------------------- Configuration ------------------- #
# Previously hardcoded to D:\Rakshit\refactoring\Dialer_again. The process
# name now comes from the folder this script lives in, and every path is built
# from that process plus the report date.
network_path = common.PROJECT_ROOT

# One structured, per-run log: Media/<process>/logs/YYYY/MM/DD/<ts>_<run_id>.log
ctx = start_run(__file__, _target_date)
paths = ctx.paths
handler = CentralErrorHandler(ctx)
log_file_path = ctx.log_path
current_date = datetime.now().strftime(common.DATE_FORMAT)


# ------------------- Logging ------------------- #
def log(message):
    ctx.info(str(message).replace("\n", " "), stage=Stage.PROCESSING)

# ------------------- Connect to Network Share ------------------- #
# try:
#     subprocess.run(["net", "use", network_path], check=True, shell=True)
#     log("Connected to network share successfully.")
# except subprocess.CalledProcessError as e:
#     log(f"Failed to connect to share drive: {e}")
#     exit(1)

# ------------------- Prepare Paths ------------------- #
# yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

#############################


# Accept optional date argument (YYYY-MM-DD format)


d = paths.date_key
# The raw folder holds the report as .csv rather than .xls.
file_name = paths.report_name("csv")
start_time = time.time()
# ------------------- Set Specific Date ------------------- #
# target_date = '2025-06-30'  # <-- change this date as needed (format: YYYY-MM-DD)
# file_name = f"{target_date}_APR.xls"

# Both folders are partitioned by report date and created on demand.
source_dir = paths.dataset(common.FOLDER_APR_RAW)
target_dir = paths.dataset(common.FOLDER_APR_CLEAN)
source_path = os.path.join(source_dir, file_name)
csv_path = os.path.join(target_dir, file_name)

# ------------------- Copy File ------------------- #
try:
    if os.path.exists(source_path):
        shutil.copy2(source_path, csv_path)
        log(f"Copied {os.path.basename(source_path)}")
        ctx.detail(f"{ctx.relative(source_path)} -> {ctx.relative(csv_path)}")
    else:
        ctx.fail(f"Source file does not exist: {ctx.relative(source_path)}")
        exit(1)
except Exception as e:
    ctx.fail(f"File copy failed | Reason: {e}")
    exit(1)

# ------------------- Verify the copied CSV ------------------- #
try:
    df = pd.read_csv(csv_path)
    log(f"Loaded {len(df)} rows")
except Exception as e:
    ctx.fail(f"Could not read CSV | Reason: {e}")
    exit(1)


# ------------------

try:
    data = pd.read_csv(csv_path)
    # updated_data = data.drop(index=range(0, 67))  # Drop rows 2-7 (index 1-6)
    # updated_data.to_csv(csv_path, index=False)
    data.to_csv(csv_path, index=False)
    ctx.detail("Updated CSV saved")
except Exception as e:
    ctx.fail(f"CSV processing failed | Reason: {e}")

def time_to_minutes(time_str):
    if isinstance(time_str, str):  # Check if the value is a string
        try:
            h, m, s = map(int, time_str.split(":"))
            return h * 60 + m + s / 60
        except ValueError:
            return 0  # Handle invalid or empty time strings
    return 0  # Return 0 for non-string values

try:
    # Load the CSV file
    data = pd.read_csv(csv_path)

    # Ensure required columns exist
    if "Login Duration" in data.columns and "Total Break Duration" in data.columns:
        # Convert time columns to minutes
        data["Login Duration (minutes)"] = data["Login Duration"].apply(time_to_minutes)
        data["Total Break Duration (minutes)"] = data["Total Break Duration"].apply(time_to_minutes)

        # Calculate Net Login
        data["Minutes"] = (data["Login Duration (minutes)"] - data["Total Break Duration (minutes)"])
        data = data[data["Minutes"] != 0.0]

        # Drop intermediate minute columns if not needed
        # data.drop(["Login Duration (minutes)", "Total Break Duration (minutes)"], axis=1, inplace=True)
        data["Agent ID"] = data["Agent ID"].str.strip()


        total_row_index = data[data["##"].astype(str).str.contains('Total', case=False, na=False)].index
        if len(total_row_index) > 0:  # If a "Total" row exists
            # Get the index of the first "Total" row
            total_index = total_row_index[0]

            # Remove all rows from the "Total" row onward, including the 'Total' row
            data = data.iloc[:total_index]
        
        # columns_to_keep = ["Agent ID", "First Login", "Minutes"]
        # data = data[columns_to_keep]

        # Overwrite the same file
        data.to_csv(csv_path, index=False)
        log("Net Login added")

        data = pd.read_csv(csv_path)
        total_row_index = data[data["Agent ID"].astype(str).str.contains('Total', case=False, na=False)].index
        if len(total_row_index) > 0:  # If a "Total" row exists
            # Get the index of the first "Total" row
            total_index = total_row_index[0]

            # Remove all rows from the "Total" row onward, including the 'Total' row
            data = data.iloc[:total_index]
        
        data = data[~data.iloc[:, 0].str.contains('ICAI', case=False, na=False)]
        
        # data['IsWH'] = 'N'
        # data.rename(columns={'Agent ID': 'EmpCode', 'First Login': 'Date'}, inplace=True)
        # data['EmpCode'] = data['EmpCode'].str.replace(r"\s*\(.*\)", "", regex=True).str.upper()
        # Keep only rows where EmpCode follows the pattern "ATS" + digitspip
        # data = data[data['EmpCode'].str.match(r'^ATS\d+$', na=False)]

        data['First Login'] = pd.to_datetime(data['First Login'], errors='coerce').dt.strftime('%d-%b-%y')
        # data['Minutes'] = data['Minutes'].round().astype(int)
        data['Minutes'] = np.ceil(data['Minutes']).astype(int)
        # Overwrite the same file
        data.to_csv(csv_path, index=False)
        # Now drop the intermediate calculation columns
        columns_to_drop = ['Login Duration (minutes)', 'Total Break Duration (minutes)', 'Minutes']
        data.drop(columns=columns_to_drop, inplace=True, errors='ignore')

        # Drop the first column regardless of its name
        data.drop(data.columns[0], axis=1, inplace=True)

        # Save final cleaned CSV
        data.to_csv(csv_path, index=False)
        log("Columns cleaned")
    else:
        ctx.fail("Required columns ('Login Duration', 'Total Break Duration') "
                 "are missing in the file")
except Exception as e:
    ctx.fail(f"CSV processing failed | Reason: {e}")


try:
    # Load the final cleaned CSV
    final_df = pd.read_csv(csv_path, header=None)

    # Define XLSX path
    xlsx_path = os.path.join(target_dir, paths.report_name("xlsx"))

    # Save as Excel
    final_df.to_excel(xlsx_path, index=False, header=False)
    log(f"XLSX created: {os.path.basename(xlsx_path)}")

    # Delete the CSV file
    os.remove(csv_path)
    ctx.detail(f"deleted {ctx.relative(csv_path)}")

except Exception as e:
    ctx.fail(f"XLSX conversion failed | Reason: {e}")




# # ------------------- Drop First 98 Rows, First Column, and Rows After 'admin' ------------------- #
# try:
#     df = pd.read_csv(csv_path, encoding='utf-8-sig', skiprows=98)

#     # Drop the first column (by index)
#     df.drop(df.columns[0], axis=1, inplace=True)

#     # Find the first occurrence of 'admin' in the first column
#     admin_row_index = df[df.iloc[:, 0].astype(str).str.lower().str.contains('admin')].index

#     if not admin_row_index.empty:
#         cut_off_index = admin_row_index[0]
#         df = df.iloc[:cut_off_index]
#         log(f"Dropped rows starting from index {cut_off_index} where 'admin' was found.")

#     # Save the cleaned DataFrame
#     df.to_csv(csv_path, index=False, encoding='utf-8-sig')
#     log(f"Final cleaned CSV saved:\n{csv_path}")

# except Exception as e:
#     log(f"Error during final CSV processing: {e}")

# ------------------- Upload XLSX to Server via SFTP ------------------- #
#import paramiko  # <-- For SFTP upload

server_ip = os.getenv("APR_SFTP_HOST", "172.20.122.231")
username = os.getenv("APR_SFTP_USERNAME", "iccsadmin")
password = os.getenv("APR_SFTP_PASSWORD", "Xs0a0@bdpkgo")
sftp_port = int(os.getenv("APR_SFTP_PORT", "22"))
# The remote folder mirrors the local convention: <base>/<process>. Set
# APR_SFTP_REMOTE_DIR to override the whole path.
remote_base = os.getenv("APR_SFTP_REMOTE_BASE", "")
remote_dir = os.getenv("APR_SFTP_REMOTE_DIR") or f"{remote_base.rstrip('/')}/{paths.process}"

try:
    transport = paramiko.Transport((server_ip, sftp_port))
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)

    try:
        sftp.chdir(remote_dir)
    except IOError:
        ctx.fail(f"Remote directory does not exist: {remote_dir}")
        sftp.close()
        transport.close()
        exit(1)

    remote_path = os.path.join(remote_dir, os.path.basename(xlsx_path)).replace('\\', '/')
    sftp.put(xlsx_path, remote_path)
    log(f"Uploaded {os.path.basename(remote_path)} to {server_ip}")

    sftp.close()
    transport.close()

except Exception as e:
    ctx.fail("SFTP upload failed")
    ctx.fail(f"Reason: {e}")

ctx.finish(status="completed")
