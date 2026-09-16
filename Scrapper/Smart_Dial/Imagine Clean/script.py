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
# yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

#############################


# Accept optional date argument (YYYY-MM-DD format)


d = _target_date.strftime("%Y-%m-%d")
file_name = f"{d}_APR.xls"
start_time = time.time()
# ------------------- Set Specific Date ------------------- #
# target_date = '2025-06-30'  # <-- change this date as needed (format: YYYY-MM-DD)
# file_name = f"{target_date}_APR.xls"

source_dir = os.path.join(network_path, r"media\Imagine\APR_data")
target_dir = os.path.join(network_path, r"media\Imagine Clean\dialer_data")
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


# ------------------

try:
    data = pd.read_csv(csv_path)
    # updated_data = data.drop(index=range(0, 67))  # Drop rows 2-7 (index 1-6)
    # updated_data.to_csv(csv_path, index=False)
    data.to_csv(csv_path, index=False)
    print("Updated CSV saved successfully")
except Exception as e:
    print(f"Error processing the CSV file: {e}")

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
        print(f"'Net Login' column added successfully, and changes were saved to the original file: {csv_path}")

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
        log("Dropped temporary columns, first column, and removed header. Final CSV saved.")
    else:
        print("Required columns ('Login Duration' and 'Total Break Duration') are missing in the file.")
except Exception as e:
    print(f"Error processing the CSV file: {e}")


try:
    # Load the final cleaned CSV
    final_df = pd.read_csv(csv_path, header=None)

    # Define XLSX path
    xlsx_path = csv_path.replace(".csv", ".xlsx")

    # Save as Excel
    final_df.to_excel(xlsx_path, index=False, header=False)
    log(f"Successfully converted CSV to XLSX:\n{xlsx_path}")

    # Delete the CSV file
    os.remove(csv_path)
    log(f"Deleted temporary CSV file:\n{csv_path}")

except Exception as e:
    log(f"Error during CSV to XLSX conversion or deletion: {e}")




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

server_ip = "172.20.122.231"
username = "iccsadmin"
password = "Xs0a0@bdpkgo"
# remote_dir = "/home/iccsadmin/APR_Data/Imagine/APR_Clean"
remote_dir = "/home/iccsadmin/ishita/APR_Uploads_V2/media/dialer_watch/Imagine"

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
