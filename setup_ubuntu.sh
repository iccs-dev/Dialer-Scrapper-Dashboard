#!/usr/bin/env bash
# One-shot setup for running Dialer Scrapper on Ubuntu, from a fresh clone.
#
#     git clone <repo> && cd Dialer_Scrapper_Dashboard && ./setup_ubuntu.sh
#
# Needs no root: Playwright downloads its own Chromium into
# ~/.cache/ms-playwright. Only the shared libraries that browser links against
# may need apt, and the script says so if the launch check fails.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$PROJECT_ROOT/venv"

echo "==> Project root: $PROJECT_ROOT"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: $PYTHON_BIN not found. Install it with: sudo apt install python3 python3-venv" >&2
    exit 1
fi
echo "==> Using $("$PYTHON_BIN" -V)"

# python3-venv is a separate package on Ubuntu and is missing on minimal images.
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "==> Creating virtualenv at $VENV_DIR"
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
        echo "ERROR: could not create the virtualenv." >&2
        echo "       Install the venv module with: sudo apt install python3-venv" >&2
        exit 1
    fi
else
    echo "==> Reusing existing virtualenv at $VENV_DIR"
fi

echo "==> Checking configuration"
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    if [ -f "$PROJECT_ROOT/.env.example" ]; then
        cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
        chmod 600 "$PROJECT_ROOT/.env"
        NEEDS_CREDENTIALS=1
        echo "   created .env from .env.example - credentials are blank"
    else
        echo "ERROR: neither .env nor .env.example is present." >&2
        exit 1
    fi
else
    NEEDS_CREDENTIALS=0
    echo "   .env already present, leaving it alone"
fi

echo "==> Installing Python dependencies"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.txt"

echo "==> Resolving process folders"
"$VENV_DIR/bin/python" - <<'PY'
import datetime
import os

import common

# Nothing is pre-created beyond the media root: every script builds
# Media/<process>/<folder>/<YYYY>/<MM>/<DD> for the date it is run with.
os.makedirs(common.MEDIA_DIR, exist_ok=True)
print("   media root:", common.MEDIA_DIR)

# A "job" is any script that asks common for process-scoped paths.
MARKER = "common.process_paths("
today = datetime.date.today()
for dirpath, dirnames, filenames in os.walk(common.CODE_ROOT):
    dirnames[:] = [d for d in dirnames if d not in {"venv", ".git", "__pycache__"}]
    for name in sorted(filenames):
        if not name.endswith(".py"):
            continue
        script = os.path.join(dirpath, name)
        with open(script, encoding="utf-8", errors="ignore") as handle:
            if MARKER not in handle.read():
                continue
        paths = common.process_paths(script, today)
        print("   {:<20} process={:<10} logs -> {}".format(
            name,
            paths.process,
            os.path.relpath(paths.log_file(common.script_log_name(script), create=False),
                            common.PROJECT_ROOT),
        ))
PY

echo "==> Installing the Playwright browser (downloads to ~/.cache/ms-playwright)"
# No root needed. If Chromium refuses to start because a shared library is
# missing, install the system packages once with:
#   sudo "$VENV_DIR/bin/python" -m playwright install-deps chromium
# or: sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
#         libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
#         libxfixes3 libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 libcairo2
"$VENV_DIR/bin/python" -m playwright install chromium

echo "==> Verifying the browser stack"
"$VENV_DIR/bin/python" - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    page = browser.new_page()
    page.goto("data:text/html,<title>ok</title>")
    print("   headless Chromium OK:", browser.version, "| title:", page.title())
    browser.close()
PY

echo "==> Preparing the monitoring dashboard"
(
  cd "$PROJECT_ROOT/webapp"
  "$VENV_DIR/bin/python" manage.py migrate --no-input
  # The roster comes from the existing process list; workflow steps are what
  # let combine.py and hrms.py report in.
  "$VENV_DIR/bin/python" manage.py seed_processes
  "$VENV_DIR/bin/python" manage.py seed_workflow
  "$VENV_DIR/bin/python" manage.py collectstatic --no-input >/dev/null
  echo "   static files collected"
)

echo
if [ "${NEEDS_CREDENTIALS:-0}" = "1" ]; then
  echo "=============================================================="
  echo " BEFORE ANYTHING WILL RUN: edit .env and fill in the blanks."
  echo " Every blank value is a credential - dialer logins, HRMS, SMTP,"
  echo " DJANGO_SECRET_KEY and MONITOR_API_TOKEN."
  echo "=============================================================="
  echo
fi
echo "Create a dashboard login (asks for a password):"
echo "  cd webapp && $VENV_DIR/bin/python manage.py createsuperuser"
echo
echo "Start the dashboard on http://127.0.0.1:8001 :"
echo "  cd webapp && $VENV_DIR/bin/gunicorn -c gunicorn.conf.py dialer_dashboard.asgi:application"
echo
echo "Setup complete. Run the scripts with:"
echo "  $VENV_DIR/bin/python Scrapper/Smart_Dial/Imagine/script.py [YYYY-MM-DD]"
echo "  $VENV_DIR/bin/python Scrapper/Smart_Dial/Imagine/Cleaning_Script.py [YYYY-MM-DD]"
echo "  $VENV_DIR/bin/python combine.py [YYYY-MM-DD]   # builds the upload file,"
echo "                                                 # runs hrms.py, mails the report"
echo
echo "Or run every scraper at once (one failure does not stop the others):"
echo "  $VENV_DIR/bin/python run_scrapers.py [YYYY-MM-DD]"
echo
echo "Daily status report from the structured logs and error history:"
echo "  $VENV_DIR/bin/python hrms_monitor.py [YYYY-MM-DD]"
echo
echo "To move existing flat files into the dated layout (run once, and again"
echo "after copying historical data from the Windows machine):"
echo "  $VENV_DIR/bin/python migrate_layout.py --dry-run"
echo "  $VENV_DIR/bin/python migrate_layout.py"
