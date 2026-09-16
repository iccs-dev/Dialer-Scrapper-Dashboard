"""TN CM leg b - see tn_cm_leg.py. Run via script.py, or on its own:

    python b.py 2026-09-14
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tn_cm_leg import run_leg

if __name__ == "__main__":
    sys.exit(run_leg("b", __file__))
