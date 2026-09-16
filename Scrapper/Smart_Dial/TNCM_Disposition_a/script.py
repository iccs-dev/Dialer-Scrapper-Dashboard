"""TN CM leg A has no Disposition Report - nothing to scrape here.

This file was empty in the original codebase, which read as an oversight. It
is not. TN CM is dialled on two client codes, and only leg B's login carries
a Disposition Report: leg A (CMHELPLINE, Smart Dial 3.0) offers just User
Session, CDR Report and Abandon Summary. Confirmed against the live dialer on
2026-09-16 - the Analytics menu has three entries and no disposition of any
kind.

Leg A's productive-hour data still arrives through the TN CM APR scraper; it
is only the disposition report that does not exist for this leg.

The module stays discoverable and does nothing, so run_scrapers.py keeps
listing the job and exits cleanly rather than failing nightly on a report the
dialer will never serve. If leg A is ever licensed for it, deleting this
docstring and copying TNCM_Disposition_b/script.py with leg="A" is the whole
change.
"""
hrms_monitor.py