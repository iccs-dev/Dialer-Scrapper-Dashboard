import os
import shutil
from datetime import datetime, timedelta
import subprocess
import pandas as pd
import numpy as np
from openpyxl import load_workbook
import paramiko  # <-- For SFTP upload
import sys
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
# Ensure the correct paths to your scripts are used
date_arg = _target_date.strftime("%Y-%m-%d")

script_dir = os.path.dirname(os.path.abspath(__file__))
python_path = r"D:\Rakshit\refactoring\Dialer_again\venv\Scripts\python.exe"

p1 = subprocess.Popen([python_path, os.path.join(script_dir, 'a.py'), date_arg])
p2 = subprocess.Popen([python_path, os.path.join(script_dir, 'b.py'), date_arg])

# Wait for both processes to finish
p1.wait()
p2.wait()
if p1.returncode == 0 and p2.returncode == 0:
    print("Both scripts have completed execution.")
else:
    print("One or both scripts failed.")
# ------------------- Prepare Paths ------------------- #
d = _target_date.strftime("%Y-%m-%d")
file_name1 = f"{d}_aAPR.xlsx"
file_name2 = f"{d}_bAPR.xlsx"
combined_file = f"{d}_APR.xlsx"

source_dir = os.path.join(network_path, r"media\TN CM_Clean\dialer_data")
target_dir = os.path.join(network_path, r"media\TN CM_Clean\dialer_data")

file1_path = os.path.join(source_dir, file_name1)
file2_path = os.path.join(source_dir, file_name2)
combined_file_path = os.path.join(target_dir, combined_file)


# ------------------- Combine Files ------------------- #
try:
    file1_path = os.path.join(source_dir, file_name1)
    file2_path = os.path.join(source_dir, file_name2)
    combined_file_path = os.path.join(source_dir, f"{d}_APR.xlsx")

    if os.path.exists(file1_path) and os.path.exists(file2_path):
        # Read Excel files
        df1 = pd.read_excel(file1_path)
        df2 = pd.read_excel(file2_path)

        # Concatenate while keeping same header
        combined_df = pd.concat([df1, df2], ignore_index=True)

        # Save combined file
        combined_df.to_excel(combined_file_path, index=False)

        log(f"Files combined successfully into {combined_file_path}")

        # Delete original files
        os.remove(file1_path)
        os.remove(file2_path)
        log(f"Deleted source files: {file_name1}, {file_name2}")
    else:
        log("One or both source files are missing. Skipping combine process.")

except Exception as e:
    log(f"Error during file combine process: {e}")


# ------------------- Upload XLSX to Server via SFTP ------------------- #
#import paramiko  # <-- For SFTP upload

server_ip = "172.20.122.231"
username = "iccsadmin"
password = "Xs0a0@bdpkgo"
# remote_dir = "/home/iccsadmin/APR_Data/TN_CM/APR_Clean"
remote_dir = "/home/iccsadmin/ishita/APR_Uploads_V2/media/dialer_watch/TN_CM"

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

    remote_path = os.path.join(remote_dir, os.path.basename(combined_file_path)).replace('\\', '/')
    sftp.put(combined_file_path, remote_path)
    log(f"Uploaded file to server:\n{remote_path}")

    sftp.close()
    transport.close()

except Exception as e:
    log(f"Failed to upload file to server: {e}")