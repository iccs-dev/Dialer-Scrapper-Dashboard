import os
import sys
import shutil
from datetime import datetime, timedelta
import subprocess
import pandas as pd
import numpy as np
from openpyxl import load_workbook
import paramiko  # <-- For SFTP upload


if len(sys.argv) > 1:
    _target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d")
else:
    _target_date = datetime.today() - timedelta(days=1)
    
# ------------------- Configuration ------------------- #
network_path = r"D:\Rakshit\refactoring\Dialer_again"
log_dir = r"D:\Rakshit\refactoring\Dialer_again\LOGs\Clean_APR"
os.makedirs(log_dir, exist_ok=True)

current_date = datetime.now().strftime("%Y-%m-%d")
log_file_path = os.path.join(log_dir, f"{current_date}.log")

# ------------------- Logging ------------------- #
def log(message):
    try:
        with open(log_file_path, "a", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{timestamp}] {message}\n")
        print(message)
    except Exception as e:
        print(f"Logging failed: {e}")

# ------------------- Connect to Network Share ------------------- #
# try:
#     subprocess.run(["net", "use", network_path], check=True, shell=True)
#     log("Connected to network share successfully.")
# except subprocess.CalledProcessError as e:
#     log(f"Failed to connect to share drive: {e}")
#     exit(1)

# ------------------- Prepare Paths ------------------- #
d = _target_date.strftime("%Y-%m-%d")
file_name = f"{d}_APR.xls"     


source_dir = os.path.join(network_path, r"media\Amazon Merchant\APR_data")
target_dir = os.path.join(network_path, r"media\Amazon Merchant Clean\dialer_data")
source_path = os.path.join(source_dir, file_name)
target_path = os.path.join(target_dir, file_name)

# ------------------- Copy File ------------------- #
try:
    if os.path.exists(source_path):
        shutil.copy2(source_path, target_path)
        log(f"File copied successfully:\nFrom: {source_path}\nTo:   {target_path}")
    else:
        log(f"Source file does not exist: {source_path}")
        exit(1)
except Exception as e:
    log(f"Error during file copy: {e}")
    exit(1)

# ------------------- Convert .xls (HTML) to .csv ------------------- #
try:
    df_list = pd.read_html(target_path)
    df = df_list[0]
    csv_path = target_path.replace(".xls", ".csv")
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    log(f"Converted .xls to .csv:\n{csv_path}")
except Exception as e:
    log(f"Error during XLS to CSV conversion: {e}")
    exit(1)

# ------------------- Delete .xls File ------------------- #
try:
    os.remove(target_path)
    log(f"Deleted original .xls file: {target_path}")
except Exception as e:
    log(f"Error deleting .xls file: {e}")

# ------------------ Initial Cleanup ------------------ #
try:
    data = pd.read_csv(csv_path)
    data.to_csv(csv_path, index=False)
    print("Updated CSV saved successfully")
except Exception as e:
    print(f"Error processing the CSV file: {e}")

def time_to_minutes(time_str):
    if isinstance(time_str, str):
        try:
            h, m, s = map(int, time_str.split(":"))
            return h * 60 + m + s / 60
        except ValueError:
            return 0
    return 0

try:
    data = pd.read_csv(csv_path)

    if "Login Duration" in data.columns and "Total Break Duration" in data.columns:
        data["Login Duration (minutes)"] = data["Login Duration"].apply(time_to_minutes)
        data["Total Break Duration (minutes)"] = data["Total Break Duration"].apply(time_to_minutes)
        data["Minutes"] = data["Login Duration (minutes)"] - data["Total Break Duration (minutes)"]
        data = data[data["Minutes"] != 0.0]
        data["Agent ID"] = data["Agent ID"].str.strip()

        total_row_index = data[data["##"].astype(str).str.contains('Total', case=False, na=False)].index
        if len(total_row_index) > 0:
            total_index = total_row_index[0]
            data = data.iloc[:total_index]

        data.to_csv(csv_path, index=False)
        print(f"'Net Login' column added successfully, and changes were saved to the original file: {csv_path}")

        data = pd.read_csv(csv_path)
        total_row_index = data[data["Agent ID"].astype(str).str.contains('Total', case=False, na=False)].index
        if len(total_row_index) > 0:
            total_index = total_row_index[0]
            data = data.iloc[:total_index]

        data = data[~data.iloc[:, 0].str.contains('ICAI', case=False, na=False)]
        data['Minutes'] = np.ceil(data['Minutes']).astype(int)
        data.to_csv(csv_path, index=False)

        columns_to_drop = ['Login Duration (minutes)', 'Total Break Duration (minutes)', 'Minutes']
        data.drop(columns=columns_to_drop, inplace=True, errors='ignore')
        data.drop(data.columns[0], axis=1, inplace=True)
        data.to_csv(csv_path, index=False)
        log("Dropped temporary columns, first column, and removed header. Final CSV saved.")
    else:
        print("Required columns ('Login Duration' and 'Total Break Duration') are missing in the file.")
except Exception as e:
    print(f"Error processing the CSV file: {e}")

# ------------------- CSV to XLSX ------------------- #
try:
    final_df = pd.read_csv(csv_path, header=None)
    xlsx_path = csv_path.replace(".csv", ".xlsx")
    final_df.to_excel(xlsx_path, index=False, header=False)
    log(f"Successfully converted CSV to XLSX:\n{xlsx_path}")
    os.remove(csv_path)
    log(f"Deleted temporary CSV file:\n{csv_path}")
except Exception as e:
    log(f"Error during CSV to XLSX conversion or deletion: {e}")

# ------------------- Upload XLSX to Server via SFTP ------------------- #
server_ip = os.getenv("APR_SFTP_HOST", "")
username = os.getenv("APR_SFTP_USERNAME", "")
password = os.getenv("APR_SFTP_PASSWORD", "")
remote_dir = os.getenv("APR_SFTP_REMOTE_DIR", "")


try:
    transport = paramiko.Transport((server_ip, 22))
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)

    try:
        sftp.chdir(remote_dir)
    except IOError:
        log(f"Remote directory doesn't exist: {remote_dir}")
        sftp.close()
        transport.close()
        exit(1)

    remote_path = os.path.join(remote_dir, os.path.basename(xlsx_path)).replace('\\', '/')
    sftp.put(xlsx_path, remote_path)
    log(f"Uploaded file to server:\n{remote_path}")

    sftp.close()
    transport.close()

except Exception as e:
    log(f"Failed to upload file to server: {e}")
