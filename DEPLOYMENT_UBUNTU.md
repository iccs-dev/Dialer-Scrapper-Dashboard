# Running Dialer Scrapper on Ubuntu

The project was developed on Windows. This document lists what changed to make
it run on Ubuntu and how to deploy it.

## Quick start

```bash
cd "/path/to/Dialer Scrapper"
./setup_ubuntu.sh
```

The script creates `venv/`, installs the dependencies, creates the data and log
directories, and verifies that headless Chrome starts. It does **not** need
root.

Then run any of the scripts (each takes an optional `YYYY-MM-DD` argument and
defaults to yesterday):

```bash
venv/bin/python run_scrapers.py                                 # every scraper, failures isolated
venv/bin/python Scrapper/Smart_Dial/Imagine/Cleaning_Script.py  # clean + SFTP upload
venv/bin/python combine.py                                      # build upload file, run hrms.py, mail reports
venv/bin/python hrms_monitor.py                                 # daily status report
```

`combine.py` is the last step of the chain: it merges every process's
`dialer_data`, writes the HRMS upload file, invokes `hrms.py` (a second time
with a cleaned list if HRMS rejects any agents), and emails the dialer-data and
failed-agent reports. Run `hrms.py` directly only to re-upload an existing
file. Useful flags:

```bash
venv/bin/python combine.py 2026-09-06 --dry-run     # build only: no upload, no email
venv/bin/python combine.py 2026-09-06 --no-email    # upload but do not mail
```

## Browser automation: Playwright

Selenium has been replaced by Playwright. Browsers install into
`~/.cache/ms-playwright` and need **no root**:

```bash
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m playwright install chromium
```

`setup_ubuntu.sh` does both. If Chromium refuses to start because a shared
library is missing, install the system packages once as root:

```bash
sudo venv/bin/python -m playwright install-deps chromium
# or, explicitly:
sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2t64 libpango-1.0-0 libcairo2
```

On this machine none of that was needed - the bundled Chromium ran as-is.
The same commands work on Windows (`py -m playwright install chromium`); no
code path is OS-specific, and every path is built with `pathlib`/`os.path`
from the project root.

### Locators

Page objects live in `core/pages/`. Each element is resolved through
`core.browser.resolve()`, which tries strategies in order and **logs which one
matched**, so UI drift shows up as a warning before it becomes an outage.
Verified against the live dialer:

| Element | Strategy that matches today |
| --- | --- |
| Client code / User ID / Password | `get_by_placeholder("Enter Your Code" / "Enter UserID" / "Enter Password")` |
| Login button | `get_by_role("link", name="Log me in")` |
| User Session report | `get_by_role("link", name=/user session/i)` |
| Campaign / User dropdowns | `get_by_role("button", name=/select campaigns|select users/i)` |
| HRMS menu + Import/Save | `get_by_role("link"/"button", name=...)` |
| Analytics sidebar | `a[data-original-title='Analytics']` - icon-only, no accessible name |
| Export button | `#create-excel` - icon-only button with an empty title |

The last two have no user-facing label at all, so a stable attribute is used
deliberately rather than pretending a semantic locator exists.

### Date selection

The old code clicked `//a[text()='7']`, which could match a day belonging to
the neighbouring month. Selection now:

1. reads the picker header (`.ui-datepicker-month` / `.ui-datepicker-year`) and
   steps prev/next until **year and month** match, logging each step;
2. clicks the day inside
   `td[data-month="<m-1>"][data-year="<y>"]:not(.ui-datepicker-other-month)`,
   so an adjacent-month cell cannot be selected - and fails loudly if that
   selector matches anything other than exactly one cell;
3. re-reads the input and verifies it now starts with the target date.

```
date_selection | From Date: calendar on September 2026 after 0 step(s)
date_selection | From Date: clicking day 7 in September 2026 (data-month=8, data-year=2026, other-month cells excluded)
date_selection | From Date: verified, field now '2026-09-07 00'
```

## Error handling

```
Playwright action -> retry/recovery -> still failed?
    -> central handler -> log + screenshot + trace -> error history -> email -> next process
```

Recovery is chosen by classifying the exception (`core/errors.py`):

| Category | Action | Retried? |
| --- | --- | --- |
| timeout | retry | yes |
| network | retry | yes |
| stale/detached element | re-locate, retry | yes |
| session expired | re-login, retry | yes |
| browser failure | restart browser, re-login, retry | yes |
| UI change (no locator matched) | evidence + alert | **no** |
| invalid credentials | stop immediately + alert | **no** |
| download validation | evidence + alert | **no** |

Retry counts and limits come from `.env`: `SCRAPER_MAX_RETRIES`,
`SCRAPER_RETRY_BACKOFF`, `SCRAPER_MAX_BROWSER_RESTARTS`, `SCRAPER_MAX_RELOGINS`.

`run_scrapers.py` runs each process in its own subprocess, so a crash, hang or
hard exit in one **cannot** stop the others:

```bash
venv/bin/python run_scrapers.py 2026-09-06        # all processes
venv/bin/python run_scrapers.py --only Imagine
venv/bin/python run_scrapers.py --list
```

### Evidence

On an unrecovered failure the run writes, under the process and date:

```
Media/<process>/errors/<YYYY>/<MM>/<DD>/<run_id>/error.json
Media/<process>/errors/<YYYY>/<MM>/<DD>/<run_id>/screenshot.png
Media/<process>/errors/<YYYY>/<MM>/<DD>/<run_id>/trace.zip
Media/<process>/errors/<YYYY>/<MM>/<DD>/history.jsonl   # append-only
```

`error.json` holds the exception type and message, the failing file/function/
line **inside this project** (not Playwright internals) plus the source line,
the current URL and page title, the full traceback, the log path, and the paths
to the screenshot and trace. Open a trace with:

```bash
venv/bin/python -m playwright show-trace Media/Imagine/errors/2026/09/07/<run_id>/trace.zip
```

Toggle capture with `SCREENSHOT_ON_ERROR` and `TRACE_ON_ERROR`.

### Email alert

An unrecovered failure emails `ERROR_EMAIL_TO` (default
`digx.automation@iccs.in`) an HTML report showing Process, Target Date, Stage,
Run ID, likely cause, error message, page URL/title, origin and failing line,
log path, screenshot path, trace path and the traceback. **Credentials are
never included** - the login helpers log the field name, never the value.
Set `SCRAPER_ALERTS_ENABLED=false` to silence alerts (used for testing).

## Logs

**One combined log per process per day** covering the whole workflow:

```
Media/Imagine/Logs/2026/09/14/Imagine_2026-09-14.log
```

`YYYY/MM/DD` folders are created automatically. Scraper, Combine and HRMS all
append to this one file - there are no separate scraper/combine/hrms log files.
The status summary sits above the details and is updated as each stage runs:

```
==================================================
Imagine Daily Process Log
Date: 2026-09-14
==================================================

SCRAPER : SUCCESS
COMBINE : FAILED
HRMS    : NOT RUN

--------------------------------------------------
DETAILS
--------------------------------------------------

[10:30:01] [SCRAPER] START
[10:30:03] [SCRAPER] LOGIN - Success
[10:30:26] [SCRAPER] SUCCESS

[10:31:00] [COMBINE] START
[10:31:05] [COMBINE] ERROR - Input file not found
[10:31:05] [COMBINE] FAILED

[HRMS] NOT RUN - Combine failed
```

| Behaviour | Detail |
| --- | --- |
| Dependency | A failed stage marks every later stage `NOT RUN` **with the reason**, so a skipped stage is never mistaken for a broken one |
| Repeat runs | The file is appended, never overwritten; a repeat is separated by a `RUN STARTED: HH:MM:SS` banner, and a fresh SCRAPER pass resets the summary |
| Timestamps | Only on detail lines - never in the header or summary |
| Concurrency | `combine.py` runs `hrms.py` while still logging, so updates take an `O_EXCL` lock that works on Linux and Windows alike |
| Handlers | One console handler per run, added once, on a logger named per run - a message can never be emitted twice |
| Multiple processes | Combine and HRMS lines are written into each contributing process's daily log, so every process's file tells its whole story |

`CLEANING` (the SFTP step) appears in DETAILS but has no summary line, because
the requested summary is the three-stage workflow.

Implementation detail - locator strategies, URLs, browser version, absolute
paths - is DEBUG only and never reaches the daily log. `LOG_LEVEL=DEBUG` shows
it on the console.

## hrms_monitor.py - daily status report

Kept, with a changed role. It no longer reverse-engineers failures from
free-text logs; it reads what the pipeline now records:

```
structured logs + error history + HRMS results -> hrms_monitor.py -> daily report
```

```bash
venv/bin/python hrms_monitor.py 2026-09-07              # emails the report
venv/bin/python hrms_monitor.py 2026-09-07 --no-email   # render only
```

Writes `Media/Monitor/<date>_status.html` and `.csv` with per-process Status,
run count, which scripts ran, last stage reached, rows scraped, agents uploaded
and the issue,
plus a failure table linking each error's log, screenshot and trace. Recipients
come from `MONITOR_EMAIL_TO`, falling back to `ERROR_EMAIL_TO`.

Reused from the previous version: the report shape, HTML style and table
rendering, `summarise_reasons()`, `write_outputs()`, `send_email()`,
`subject_for()` and the "monitor failure is itself emailed" fallback. Dropped:
the `scrape.ps1` log parser, the `combine.py`/`dashboard.py` AST readers and
the Selenium exception-guessing tables - stage and category are now recorded
directly.

## Folder layout

One structure, used identically for scraped files and logs:

```
Media/<process>/<folder>/<YYYY>/<MM>/<DD>/<file>
```

```
Media/
  Imagine/                                        # process
    APR_data/2026/09/07/2026-09-07_APR.csv        # raw report (converted from the download)
    APR_Clean/2026/09/07/2026-09-07_APR.xlsx      # cleaned, uploaded via SFTP
    csv_data/2026/09/07/2026-09-07_APR.csv        # raw copy
    dialer_data/2026/09/07/2026-09-07_APR.csv     # processed (EmpCode, Date, Minutes, IsWH)
    logs/2026/09/07/script.log
    logs/2026/09/07/cleaning_script.log
  hrms/                                           # process
    upload/2026/09/07/2026-09-07.csv
    Failed_Agent_list/2026/09/07/
    logs/2026/09/07/hrms.log
```

Every segment is computed at runtime; none of it is written down in a script.

| Segment | Where it comes from |
| --- | --- |
| `<process>` | The folder the script lives in (`.../Imagine/script.py` -> `Imagine`). A script at the project root uses its own name (`hrms.py` -> `hrms`). `DIALER_PROCESS` overrides. |
| `<folder>` | A `FOLDER_*` constant in `common.py` - the single place folder names are defined. |
| `<YYYY>/<MM>/<DD>` | The report date: the `YYYY-MM-DD` argument, or yesterday. |
| file name | `ProcessPaths.report_name()` / `dated_name()`, built from the same date. |
| log file name | The script's own file name (`Cleaning_Script.py` -> `cleaning_script.log`). |

Adding a process means dropping a script into a new folder - no code or config
change. Missing folders are created on first use, for any date and any process:

```
Media/Qurex/APR_data/2026/12/31/     created automatically
Media/NHA/logs/2025/02/28/           created automatically
```

Logs are filed under the **report date**, not the run date, so a re-run for an
old date appends to that date's log rather than today's.

`hrms.py` still accepts the older flat `Media/upload/<date>.csv` if the dated
file is absent, so an existing feed into that folder keeps working.

The SFTP target follows the same convention: `APR_SFTP_REMOTE_BASE` with the
process appended, so `Imagine` uploads to `<base>/Imagine`. Set
`APR_SFTP_REMOTE_DIR` to override the full path.

### Migrating existing files

```bash
venv/bin/python migrate_layout.py --dry-run                 # preview
venv/bin/python migrate_layout.py --prune-empty
venv/bin/python migrate_layout.py --legacy-process hrms     # adopt Media/upload etc.
```

It moves date-named files sitting flat in a data folder into `YYYY/MM/DD`,
converts the raw folder's `.xls` to `.csv`, renames per-run logs written before
the script name was part of the file name (reading the script from each run's
own startup banner), and routes a shared top-level log folder to whichever
process owns each script. `--prune-empty` removes only the
legacy folders it emptied; the pre-created date folders under a process are
left alone. Run it again after copying historical data off the Windows machine.

## What was changed and why

### 1. Dependencies (`requirements.txt`)

The original pins could not be installed on Ubuntu:

| Package | Problem |
| --- | --- |
| `pywin32==308` | Windows-only; no Linux distribution exists at all. |
| `numpy==2.2.2`, `pandas==2.2.3`, `lxml==5.3.0` | No wheels for the Python 3.12+ interpreters on current Ubuntu releases (this machine ships Python 3.14), so pip fell back to building them from source. |

`requirements.txt` now lists only the direct dependencies with lower bounds and
leaves the transitive ones (certifi, cffi, cryptography, PyNaCl, trio, urllib3,
…) to pip. `pywin32` is kept behind a `sys_platform == "win32"` marker, so the
same file still works on Windows. The original file is preserved as
`requirements.windows-original.txt` for reference.

### 2. Hardcoded Windows paths

`Cleaning_Script.py` pointed at `D:\Rakshit\refactoring\Dialer_again` and joined
sub-paths with backslashes (`media\Imagine\APR_data`), which on Linux become
part of the filename rather than directory separators. All paths are now
derived from the project root.

### 3. Case-sensitive filesystem

The scripts referred to `media/...` while the directory on disk is `Media/...`.
Windows treats these as the same directory; Linux does not, so the scripts would
have silently created a second, empty tree. Path resolution now matches an
existing directory case-insensitively.

### 4. Chrome and chromedriver

`ChromeDriverManager().install()` only fetches the *driver* — it assumes a Chrome
*browser* is already installed, which is not true on a fresh Ubuntu server.
Driver startup now tries, in order:

1. Selenium Manager (built into Selenium ≥ 4.6). It matches the driver to the
   installed browser, and downloads a private "Chrome for Testing" build into
   `~/.cache/selenium` when no browser is present — **no root required**.
2. A `chromedriver` already on `PATH` (or `CHROMEDRIVER_PATH`).
3. `webdriver-manager`, as before.

Set `CHROME_BINARY` / `CHROMEDRIVER_PATH` in `.env` to force specific binaries.
If you prefer a system-wide browser, install one as root:

```bash
sudo apt install -y chromium-browser        # snap-backed on Ubuntu 24.04+
# or Google Chrome:
wget -qO /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y /tmp/chrome.deb
```

### 5. Headless window size

`--start-maximized` does nothing headless on Linux (there is no window manager),
leaving an 800x600 viewport that can push elements out of view and break clicks.
An explicit `--window-size=1920,1080` is now always set, and headless mode uses
Chrome's `--headless=new`.

### 6. `hrms.py` crash masking

If Chrome failed to start, the `finally:` block raised `NameError: driver` and
hid the real error. `driver` is now initialised to `None` and guarded.

### 7. Blind 5-minute sleep after the export

`script.py` clicked "create excel", slept a fixed `5*60` seconds, then looked
for a file called `user_session_DYSON.xls`. Both are gone: the folder is
snapshotted before the click and `common.wait_for_download()` polls for
whatever new, non-partial (`.crdownload`) file arrives, up to the same 5-minute
ceiling. A run that used to take 320s now finishes in ~36s when the report is
ready quickly, and it no longer breaks if the dialer renames its export.

## Shared helper: `common.py`

New module at the project root holding the cross-platform bits:

- `FOLDER_*` constants - the only place folder names are written down
- `detect_process(__file__)` / `script_log_name(__file__)` - runtime identity
- `process_paths(__file__, date)` -> `ProcessPaths`, with `.dataset(folder)`,
  `.file(folder, name)`, `.logs()`, `.log_file(name)`, `.report_name(ext)`,
  `.dated_name(ext)` - all creating folders on demand
- `wait_for_download(folder, ignore=...)` - waits for a download by arrival
  rather than by a known file name
- `load_env()` — loads `.env` from the project root
- `find_chrome_binary()`, `find_chromedriver()`
- `build_chrome_options()`, `create_chrome_driver()`

Set `DIALER_SCRAPPER_ROOT` to run the code against a different data root (used
for testing, or when the code lives in `/opt` and data lives elsewhere), and
`DIALER_PROCESS` to file output under a process other than the auto-detected
one.

## Scheduling

The Windows Task Scheduler jobs map onto cron. Each script writes its own
dated log under `Media/log/`, so the redirect below only catches startup
failures. Example — scrape at 01:00, clean
and upload at 02:00, push to HRMS at 03:00:

```cron
0 1 * * * cd "/home/vboxuser/Dialer Scrapper" && venv/bin/python Scrapper/Smart_Dial/Imagine/script.py >> Media/log/cron.log 2>&1
0 2 * * * cd "/home/vboxuser/Dialer Scrapper" && venv/bin/python Scrapper/Smart_Dial/Imagine/Cleaning_Script.py >> Media/log/cron.log 2>&1
0 3 * * * cd "/home/vboxuser/Dialer Scrapper" && venv/bin/python combine.py >> Media/log/cron.log 2>&1
```

## Still outstanding

- **`.env` holds plaintext credentials** (HRMS, dialer, SFTP, share drive) and
  the dialer login in `script.py` is still hardcoded in source. Unrelated to the
  port, but worth moving to a secrets store before wider deployment. Make sure
  `.env` is `chmod 600` on the server.
- **`Cleaning_Script.py` depends on the `##` column being non-numeric.** The
  ICAI filter uses `data.iloc[:, 0].str.contains(...)`, i.e. the `##` column
  rather than `Agent ID`. Real exports carry text in that column, so it works
  (verified against live data), but on a report where `##` is purely numeric it
  raises `Can only use .str accessor with string values!`. The surrounding
  `try/except` swallows it, which would silently skip the ICAI filter, the date
  reformat, the rounding and the column drops. Testing `Agent ID` instead of
  column 0 would remove the dependency; left alone because it changes the
  uploaded file.
