from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import win32com.client as win32
import subprocess
import sys
import time
import os
import paramiko
# from datetime import datetime
from datetime import datetime, timedelta

# Accept optional date argument (YYYY-MM-DD) from command line
if len(sys.argv) > 1:
    _target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d")
else:
    _target_date = datetime.today() - timedelta(days=1)

import pandas as pd  #install
import numpy as np  #install
import shutil
from screeninfo import get_monitors

start_time = time.time()

service = Service(ChromeDriverManager().install())

script_dir = os.path.dirname(os.path.abspath(__file__))
DMI = os.path.dirname(script_dir)
Smart_Dial = os.path.dirname(DMI)
Scrapper = os.path.dirname(Smart_Dial)

# Define paths inside 'Media/ICAI'
media_dmi_dir = os.path.join(Scrapper, "media", "DMI")
disposition_data_dir = os.path.join(media_dmi_dir,"disposition_data")

# csv_data_dir = os.path.join(media_icai_dir, "csv_data")
# os.makedirs(csv_data_dir, exist_ok=True)
os.makedirs(disposition_data_dir,exist_ok=True)
# Create Clean_disposition_data directory
clean_dir = os.path.join(media_dmi_dir, "Clean_disposition_data")
os.makedirs(clean_dir, exist_ok=True)
# Create the 'media' folder if it doesn't exist


# ---------------------------------------------------------------------------
# Download helpers
#
# Chrome holds downloads it considers unsafe (an .xlsx is a zip archive served
# over plain HTTP) as "Unconfirmed NNNN.crdownload" until the user clicks
# "Keep" in the download bubble. That bubble is browser UI, not page DOM, so it
# can only be driven through chrome://downloads. Everything below either stops
# the prompt from appearing or clicks Keep when it does.
# ---------------------------------------------------------------------------

def purge_stale_crdownloads(folder):
    """Delete leftover .crdownload files from previous runs that stalled on Keep."""
    removed = 0
    for name in os.listdir(folder):
        if name.endswith(".crdownload"):
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError as exc:
                print(f"[DMI_Disposition] Could not delete {name}: {exc}")
    if removed:
        print(f"[DMI_Disposition] Cleaned {removed} stale .crdownload file(s)")


# Walks every open shadow root on chrome://downloads and clicks any Keep /
# "save dangerous" control it finds. Element ids on that page change between
# Chrome versions, so match on id OR visible text instead of one fixed
# selector, and never touch anything that discards the file.
_CLICK_KEEP_JS = r"""
const clicked = [];
const KEEP_ID = /save-dangerous|keep|download-dangerous|save-anyway/i;
const KEEP_TEXT = /^(keep|keep anyway|keep dangerous file|download dangerous file|save anyway|continue|proceed)$/i;
const DISCARD = /discard|delete|remove|cancel/i;

function* walk(root) {
  for (const node of root.querySelectorAll('*')) {
    yield node;
    if (node.shadowRoot) yield* walk(node.shadowRoot);
  }
}

for (const el of walk(document)) {
  const tag = el.tagName;
  if (!/^(CR-BUTTON|BUTTON|PAPER-BUTTON|CR-ICON-BUTTON|A)$/.test(tag)) continue;
  if (!el.getClientRects().length) continue;

  const id = el.id || '';
  const text = (el.textContent || '').trim();
  const label = el.getAttribute('aria-label') || '';

  if (DISCARD.test(id) || DISCARD.test(text) || DISCARD.test(label)) continue;
  if (!(KEEP_ID.test(id) || KEEP_TEXT.test(text) || KEEP_TEXT.test(label))) continue;

  el.click();
  clicked.push(id || text || label);
}
return clicked;
"""


def click_keep_in_downloads(driver):
    """Open chrome://downloads in a scratch tab and click Keep on held downloads.

    Returns the list of controls clicked. Newer Chrome versions show a second
    confirmation interstitial after the first click, so click in two passes.
    """
    original_handle = driver.current_window_handle
    original_handles = set(driver.window_handles)
    clicked = []
    try:
        driver.switch_to.new_window("tab")
        driver.get("chrome://downloads/")
        time.sleep(2)
        for _ in range(2):  # pass 1 = Keep, pass 2 = confirmation interstitial
            hits = driver.execute_script(_CLICK_KEEP_JS) or []
            clicked.extend(hits)
            if not hits:
                break
            time.sleep(2)
    except Exception as exc:
        print(f"[DMI_Disposition] Keep click failed: {exc}")
    finally:
        try:
            for handle in driver.window_handles:
                if handle not in original_handles:
                    driver.switch_to.window(handle)
                    driver.close()
            driver.switch_to.window(original_handle)
        except Exception as exc:
            print(f"[DMI_Disposition] Could not restore original tab: {exc}")

    if clicked:
        print(f"[DMI_Disposition] Clicked Keep control(s): {clicked}")
    return clicked


def _size_of(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def wait_for_download(driver, folder, before, timeout=180, stall_seconds=12):
    """Wait for a NEW finished file to appear in `folder`.

    `before` is the set of filenames captured just before the export click, so
    stale output from earlier runs can never be mistaken for a fresh download.
    If a .crdownload stops growing, Chrome is waiting on Keep - click it. If
    the prompt never clears, promote the fully downloaded .crdownload instead
    of losing the run.
    """
    deadline = time.time() + timeout
    last_sizes = {}
    stalled_since = None
    keep_attempts = 0

    while time.time() < deadline:
        new_names = set(os.listdir(folder)) - before
        finished = [n for n in new_names if not n.endswith((".crdownload", ".tmp"))]
        if finished:
            newest = max(
                (os.path.join(folder, n) for n in finished), key=os.path.getctime
            )
            # Make sure Chrome is done writing before handing the path back.
            size = _size_of(newest)
            time.sleep(2)
            if size == _size_of(newest) and size > 0:
                print(f"[DMI_Disposition] Download finished: {newest}")
                return newest
            continue

        pending = [n for n in new_names if n.endswith(".crdownload")]
        if pending:
            sizes = {n: _size_of(os.path.join(folder, n)) for n in pending}
            if sizes == last_sizes and all(v > 0 for v in sizes.values()):
                stalled_since = stalled_since or time.time()
                if time.time() - stalled_since >= stall_seconds and keep_attempts < 3:
                    keep_attempts += 1
                    print(
                        f"[DMI_Disposition] Download held by Chrome "
                        f"(attempt {keep_attempts}/3) - clicking Keep..."
                    )
                    click_keep_in_downloads(driver)
                    stalled_since = time.time()
            else:
                stalled_since = None
            last_sizes = sizes

        time.sleep(2)

    # Last resort: the bytes are already on disk, only the Keep decision is
    # missing. Promote the stable .crdownload rather than failing the run.
    leftover = [n for n in set(os.listdir(folder)) - before if n.endswith(".crdownload")]
    if leftover:
        newest = max((os.path.join(folder, n) for n in leftover), key=os.path.getctime)
        size = _size_of(newest)
        time.sleep(3)
        if size > 0 and size == _size_of(newest):
            recovered = os.path.join(folder, "disposition.xlsx")
            if os.path.exists(recovered):
                os.remove(recovered)
            os.replace(newest, recovered)
            print(
                f"[DMI_Disposition] WARNING: Keep prompt never cleared. "
                f"Recovered stalled download -> {recovered}"
            )
            return recovered

    return None


purge_stale_crdownloads(disposition_data_dir)


prefs = {
    "profile.default_content_settings.popups": 0,
    "download.default_directory":  disposition_data_dir,  # Set download directory to script folder
    "download.prompt_for_download": False,  # Disable the download prompt
    "download.directory_upgrade": True,  # Upgrade the directory if necessary
    "download_restrictions": 0,  # 0 = no download blocking policy
    "download_bubble.partial_view_enabled": False,  # Don't pop the download bubble
    "safebrowsing.enabled": False,  # OFF - Safe Browsing is what holds the file for "Keep"
    "safebrowsing.disable_download_protection": True,  # Legacy pref, harmless on new Chrome
    # "plugins.always_open_pdf_externally": True,
}
width, height = 1920, 1080
options = Options()
options.add_argument("--start-maximized")  
options.add_argument("--allow-insecure-localhost")  # Allow insecure localhost
options.add_argument("--ignore-certificate-errors")  # Ignore certificate errors
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-extensions")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("--allow-running-insecure-content")
# The dialer lives on .100 - the old .35 here was copied from another scrapper
options.add_argument("--unsafely-treat-insecure-origin-as-secure=http://172.20.122.100")
options.add_argument("--disable-web-security")
options.add_argument("--safebrowsing-disable-download-protection")
options.add_argument("--disable-features=InsecureDownloadWarnings,DownloadBubble,DownloadBubbleV2")
options.add_argument(f"--window-size={width},{height}")
options.add_argument("--headless")
options.add_experimental_option("prefs", prefs)



driver = webdriver.Chrome(service=service, options=options)
# driver = webdriver.Chrome(service=service, options=options)

driver.get("http://172.20.122.100/smart/index.php#")
 
# Fill in the client code
client_code_input = driver.find_element(By.ID, "code")  
client_code_input.send_keys("3050")

# Fill in the username
username_input = driver.find_element(By.ID, "username")  
username_input.send_keys("MIS")

# Fill in the password
password_input = driver.find_element(By.ID, "password")  
password_input.send_keys("Noida@1274")

login_button = driver.find_element(By.ID, "Submit")  
login_button.click() 

analytics_button = WebDriverWait(driver, 10).until(
    EC.visibility_of_element_located((By.XPATH, "//a[@data-original-title='Analytics']"))
)
analytics_button.click()

crm_report_button = driver.find_element(By.XPATH, "//a[contains(text(), 'Disposition Report')]")  
crm_report_button.click()

# time.sleep(5)
# iframe = WebDriverWait(driver, 10).until(
#     EC.frame_to_be_available_and_switch_to_it((By.ID, "sub-frame"))
# )

# WebDriverWait(driver, 10).until(
#     EC.presence_of_element_located((By.XPATH, "//table[@class='header']//tr[1]"))  
# )

# campaign_dropdown = WebDriverWait(driver, 10).until(
#     EC.element_to_be_clickable((By.XPATH, "//div[@class='SumoSelect']"))
# )
# campaign_dropdown.click()


# # Click on "Select All" checkbox
# select_all_checkbox = driver.find_element(By.XPATH, "//p[@class='select-all']")
# select_all_checkbox.click()

# # Optional: Click OK button to confirm selection
# ok_button = driver.find_element(By.XPATH, "//p[@class='btnOk']")
# ok_button.click()
time.sleep(5)
iframe = WebDriverWait(driver, 10).until(
    EC.frame_to_be_available_and_switch_to_it((By.ID, "sub-frame"))
)

WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.XPATH, "//table[@class='header']//tr[1]"))  
)

campaign_dropdown = WebDriverWait(driver, 10).until(
    EC.element_to_be_clickable((By.XPATH, "//div[@class='SumoSelect']"))
)
campaign_dropdown.click()

WebDriverWait(driver, 10).until(
    EC.element_attribute_to_include((By.XPATH, "//div[@class='SumoSelect']"), "aria-expanded")
)

dropdown_container = WebDriverWait(driver, 10).until(
    EC.visibility_of_element_located((By.XPATH, "//ul[@class='options']"))
)

option_to_select = WebDriverWait(driver, 10).until(
    # Campaign edit here
    EC.element_to_be_clickable((By.XPATH, "//p[@class='select-all']"))
)
option_to_select.click() 

wait = WebDriverWait(driver, 10)

ok_button = wait.until(
    EC.element_to_be_clickable((By.XPATH, "//p[contains(@class, 'btnOk')]"))
)
ok_button.click()

WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.XPATH, "//table[@class='header']//tr[1]")) 
)
######## User Click #################
campaign_dropdown = WebDriverWait(driver, 10).until(
    EC.element_to_be_clickable((By.XPATH, ".//td[2]//div[@class='SumoSelect']"))
)
campaign_dropdown.click()

WebDriverWait(driver, 10).until(
    EC.element_attribute_to_include((By.XPATH, "//div[@class='SumoSelect']"), "aria-expanded")

)

WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.XPATH, ".//td[2]//ul[@class='options']"))
)

dropdown_container = WebDriverWait(driver, 10).until(
    EC.visibility_of_element_located((By.XPATH, ".//td[2]//ul[@class='options']"))
)

option_to_select = WebDriverWait(driver, 10).until(
    # Lead edit here
    EC.element_to_be_clickable((By.XPATH, ".//td[2]//p[@class='select-all selected']//label[text()='Select All']"))
)
option_to_select.click()
option_to_select.click()  
# time.sleep(2)

wait = WebDriverWait(driver, 10)

ok_button = wait.until(
    EC.element_to_be_clickable((By.XPATH, ".//td[2]//p[contains(@class, 'btnOk')]"))
)
ok_button.click()

#---------------------------------------------------------


#---------------------------------------------------------

yesterday_date = _target_date
current_date = _target_date + timedelta(days=1)

# Open the calendar input field
calendar_button = driver.find_element(By.XPATH, "//td[4]//input[@id='date1']")
calendar_button.click()

# Wait for the calendar to appear
calendar = WebDriverWait(driver, 10).until(
    EC.visibility_of_element_located((By.ID, "ui-datepicker-div"))
)

# Function to get the numeric representation of the month
def month_to_number(month_name):
    return datetime.strptime(month_name, "%B").month


def navigate_calendar(driver, desired_year, desired_month, desired_day):
    """Navigate the jQuery UI datepicker to the desired date and click the day."""
    desired_month_num = month_to_number(desired_month)

    # Bound the loop so a mis-read header can never spin forever
    for _ in range(120):
        displayed_month = driver.find_element(By.CLASS_NAME, "ui-datepicker-month").text
        displayed_year = int(driver.find_element(By.CLASS_NAME, "ui-datepicker-year").text)
        displayed_month_num = month_to_number(displayed_month)

        if displayed_year == desired_year and displayed_month_num == desired_month_num:
            break

        # Compare month NUMBERS, not month names (names sort alphabetically)
        if displayed_year > desired_year or (displayed_year == desired_year and displayed_month_num > desired_month_num):
            driver.find_element(By.CLASS_NAME, "ui-datepicker-prev").click()
        else:
            driver.find_element(By.CLASS_NAME, "ui-datepicker-next").click()
        time.sleep(0.5)
    else:
        raise Exception(f"Could not navigate calendar to {desired_month} {desired_year}")

    # Scope the day lookup to the open datepicker so no other link on the page matches
    day_button = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable(
            (By.XPATH, f"//div[@id='ui-datepicker-div']//a[text()='{desired_day}']")
        )
    )
    day_button.click()


navigate_calendar(
    driver,
    yesterday_date.year,
    yesterday_date.strftime("%B"),
    str(yesterday_date.day),
)


#--------------------------------------------------------

# time.sleep(4)

calendar_button = driver.find_element(By.XPATH, "//td[5]//input[@id='date2']")
calendar_button.click()

# Wait for the calendar to appear
calendar = WebDriverWait(driver, 10).until(
    EC.visibility_of_element_located((By.ID, "ui-datepicker-div"))
)

navigate_calendar(
    driver,
    current_date.year,
    current_date.strftime("%B"),
    str(current_date.day),
)

#-------------------------------------------------------------------








# time.sleep(4)

# Snapshot the folder BEFORE exporting so an old file can never be mistaken
# for this run's download.
files_before_download = set(os.listdir(disposition_data_dir))

create_excel_button = WebDriverWait(driver, 10).until(
    EC.element_to_be_clickable((By.ID, "create-excel"))
)
create_excel_button.click()
print("[DMI_Disposition] Export clicked, waiting for download...")

latest_file = wait_for_download(driver, disposition_data_dir, files_before_download)

if not latest_file:
    print("[DMI_Disposition] Download did not complete in time.")
    driver.quit()
    sys.exit(1)

# Format yesterday's date as dd-mm-yyyy
# yesterday_str = yesterday_date.strftime("%d-%m-%Y")
# new_filename = f"Disposition_report_{yesterday_str}.xlsx"
# new_filepath = os.path.join(disposition_data_dir, new_filename)

# # Rename the downloaded file
# os.rename(latest_file, new_filepath)
# print(f"File saved as: {new_filepath}")





current_date = (_target_date + timedelta(days=1)).strftime("%Y-%m-%d")
d = _target_date.strftime("%Y-%m-%d")
new_xls_name = f"{d}.xlsx"

# Process exactly the file this run downloaded
clean_csv_path = None
original_file_path = latest_file
renamed_file_path = os.path.join(disposition_data_dir, new_xls_name)

if os.path.exists(renamed_file_path):
    os.remove(renamed_file_path)

os.replace(original_file_path, renamed_file_path)
print(f"[DMI_Disposition] Saved download as: {renamed_file_path}")


def repair_excel_with_excel(filepath):
    excel = win32.DispatchEx("Excel.Application")
    excel.DisplayAlerts = False
    wb = excel.Workbooks.Open(filepath, CorruptLoad=1)  # 1 = xlRepairFile
    clean_path = filepath.replace(".xlsx", "_cleaned.xlsx")
    wb.SaveAs(clean_path, FileFormat=51)  # 51 = xlOpenXMLWorkbook (.xlsx)
    wb.Close(False)
    excel.Quit()
    return clean_path


# Repair the corrupted file
cleaned_file_path = repair_excel_with_excel(renamed_file_path)


# The dialer leaves Sub Disposition blank when the agent selected only a top
# level disposition. Downstream consumers need a value there rather than an
# empty cell, so say so explicitly. Only blanks are touched - a real
# sub-disposition is always preserved.
SUB_DISPOSITION_PLACEHOLDER = "No Sub Disposition"


def fill_sub_disposition(frame):
    """Replace blank/whitespace-only Sub Disposition cells with the placeholder."""
    if "Sub Disposition" in frame.columns:
        column = frame["Sub Disposition"].fillna("").astype(str).str.strip()
        frame["Sub Disposition"] = column.mask(column == "", SUB_DISPOSITION_PLACEHOLDER)
    return frame


clean_csv_dir = os.path.join(media_dmi_dir, "Clean_disposition")
os.makedirs(clean_csv_dir, exist_ok=True)

try:
    csv_data = pd.read_excel(cleaned_file_path, engine='openpyxl', dtype=str)
    csv_data = fill_sub_disposition(csv_data)
    clean_csv_path = os.path.join(clean_csv_dir, f"{d}_Disposition.csv")

    # 5. Save as .xlsx
    csv_data.to_csv(clean_csv_path, index=False)
    print(f"Cleaned file saved to: {clean_csv_path}")

except Exception as e:
    print(f"Error converting to csv {e}")


try:
    # Read the .xlsx file as text to prevent auto-conversion errors
    xlsx_data = pd.read_excel(cleaned_file_path, engine='openpyxl', dtype=str)

    # Replace known problematic values with np.nan
    xlsx_data.replace(['INF', 'inf', '-INF', 'NaN', 'nan', ''], np.nan, inplace=True)

    safe_numeric_cols = ['Prefix', 'Phone Numbr', 'DID Number']
    for col in safe_numeric_cols:
        if col in xlsx_data.columns:
            xlsx_data[col] = pd.to_numeric(xlsx_data[col], errors='coerce')
    if 'Unique ID' in xlsx_data.columns:
        xlsx_data['Unique ID'] = xlsx_data['Unique ID'].astype(str)

    # Replace 'INF' and other problematic values with an empty string or a valid number
    # xlsx_data.replace(['INF', 'inf', '-INF', 'NaN', 'nan'], '', inplace=True)
    # 1. Keep only columns up to 'Unique ID'
    if 'Unique ID' in xlsx_data.columns:
        idx = xlsx_data.columns.get_loc('Unique ID')
        xlsx_data = xlsx_data.iloc[:, :idx + 1]
        xlsx_data = fill_sub_disposition(xlsx_data)
    else:
        raise Exception("'Unique ID' column not found in the file.")

    # 2. Remove the headers (convert DataFrame to array, excluding headers)
    data_only = xlsx_data.values.tolist()

    # 3. Convert back to a DataFrame without headers
    no_header_df = pd.DataFrame(data_only)

    # 4. Define cleaned .xlsx save path
    clean_xlsx_path = os.path.join(clean_dir, f"{d}_Clean_Disposition.xlsx")

    # 5. Save as .xlsx
    no_header_df.to_excel(clean_xlsx_path, header=False, index=False)
    print(f"Cleaned file saved to: {clean_xlsx_path}")

    # 6. Remove the intermediate repaired file
    os.remove(cleaned_file_path)
    print(f"Deleted original .xlsx file: {cleaned_file_path}")


except Exception as e:
    print(f"Error during conversion: {e}")


server_ip = "172.20.122.231"
username = "iccsadmin"
password = "Xs0a0@bdpkgo"
remote_dir = "/home/iccsadmin/ishita/APR_Uploads_V2/media/Disposition_Data/DMI_Disposition"

def ensure_remote_dir(sftp, remote_directory):
    """Ensure remote directory exists, create if not"""
    dirs = remote_directory.strip("/").split("/")
    path = ""
    for d in dirs:
        path = f"{path}/{d}"
        try:
            sftp.chdir(path)
        except IOError:
            # Directory does not exist, so create it
            sftp.mkdir(path)
            sftp.chdir(path)

try:
    if not clean_csv_path or not os.path.exists(clean_csv_path):
        raise Exception("No cleaned CSV was produced - nothing to upload")

    transport = paramiko.Transport((server_ip, 22))
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)

    # Ensure directory exists (create if missing)
    ensure_remote_dir(sftp, remote_dir)

    # Upload file
    remote_path = os.path.join(remote_dir, os.path.basename(clean_csv_path)).replace("\\", "/")
    sftp.put(clean_csv_path, remote_path)
    print(f"Uploaded file to server:\n{remote_path}")

    sftp.close()
    transport.close()

except Exception as e:
    print(f"Failed to upload file to server: {e}")

driver.quit()
end_time = time.time()
total_time = end_time - start_time
print(f"Total time taken for execution: {total_time} seconds")
