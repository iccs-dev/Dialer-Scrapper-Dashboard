"""Page object for the Smart Dial dialer (http://<host>/smart/).

Locator strategy, in the order each is tried:

1. semantic / user-facing - ``get_by_placeholder``, ``get_by_role``,
   ``get_by_text``. The login inputs really do carry placeholders
   ("Enter Your Code", "Enter UserID", "Enter Password"), the submit control is
   a link reading "Log me in", the report link reads "USER SESSION", and each
   campaign/user dropdown is a ``div[role=button]`` whose accessible name is
   its placeholder ("Select campaigns" / "Select users").
2. structural but meaningful - e.g. the ``SumoSelect`` wrapper *containing*
   ``select#campaign``.
3. the original id/XPath, kept as a last resort so a UI tweak degrades instead
   of failing outright.

Every fallback that gets used is logged as a warning, so drift is visible
before it becomes an outage.

Two elements have no semantic handle at all and use their id deliberately:
the Analytics sidebar link (an icon-only ``<a>`` whose only label is
``data-original-title``) and ``#create-excel`` (an icon-only button with an
empty title).
"""

import re
from pathlib import Path

from core.browser import resolve
from core.errors import (
    DownloadValidationError,
    InvalidCredentialsError,
    UiChangedError,
)
from core.runlog import Stage

#: Columns the User Session export must contain to be usable downstream.
REQUIRED_COLUMNS = ("Agent ID", "Login Duration", "Total Break Duration")

#: Smallest plausible export. Anything less is an error page, not a report.
MIN_DOWNLOAD_BYTES = 512


class SmartDialPage:
    """Drives one Smart Dial session.

    Login, the Analytics menu, the SumoSelect dropdowns and the datepicker are
    identical across this dialer's reports, so a report subclass only has to
    declare which link opens it, which date inputs it uses and what its export
    must contain.
    """

    #: The Analytics menu entry that opens this report.
    REPORT_NAME = r"user\s*session"
    #: Human label for logs.
    REPORT_LABEL = "User Session"
    #: From/To datepicker input ids - they differ between reports.
    DATE_FIELDS = {"from": "date1", "to": "date"}
    #: Columns the export must contain to be usable downstream.
    REQUIRED_COLUMNS = REQUIRED_COLUMNS

    def __init__(self, session, ctx, runner, base_url, client_code, username, password):
        self.session = session
        self.ctx = ctx
        self.runner = runner
        self.base_url = base_url.rstrip("#")
        self.client_code = client_code
        self.username = username
        self.password = password

    @property
    def page(self):
        return self.session.page

    @property
    def frame(self):
        """The report iframe. Re-resolved each time so it survives a reload."""
        return self.page.frame_locator("#sub-frame")

    # ------------------------------------------------------------------ login

    def login(self):
        """Authenticate. Raises InvalidCredentialsError if the dialer says no."""
        with self.ctx.stage_scope(Stage.LOGIN):
            self.runner.run(
                "open login page",
                lambda: self.page.goto(self.base_url, wait_until="domcontentloaded"),
                stage=Stage.LOGIN,
            )
            self.ctx.detail(f"Login page loaded: {self.page.url}")

            fields = [
                ("client code", "Enter Your Code", "#code", self.client_code),
                ("user id", "Enter UserID", "#username", self.username),
                ("password", "Enter Password", "#password", "***"),
            ]
            values = {"client code": self.client_code, "user id": self.username,
                      "password": self.password}
            for label, placeholder, fallback_id, _shown in fields:
                locator = resolve(
                    self.page,
                    [
                        (f"get_by_placeholder({placeholder!r})",
                         lambda s, p=placeholder: s.get_by_placeholder(p)),
                        (f"get_by_label(/{label}/i)",
                         lambda s, l=label: s.get_by_label(re.compile(l, re.I))),
                        (f"css {fallback_id}", lambda s, f=fallback_id: s.locator(f)),
                    ],
                    self.ctx, f"login field '{label}'",
                )
                locator.fill(values[label])
                # Never log the value itself.
                self.ctx.detail(f"filled login field '{label}'")

            submit = resolve(
                self.page,
                [
                    ("get_by_role(link, 'Log me in')",
                     lambda s: s.get_by_role("link", name=re.compile(r"log\s*me\s*in", re.I))),
                    ("get_by_role(button, /login|sign in/i)",
                     lambda s: s.get_by_role("button", name=re.compile(r"log\s*in|sign\s*in", re.I))),
                    ("css #Submit", lambda s: s.locator("#Submit")),
                ],
                self.ctx, "login submit",
            )
            submit.click()
            self._confirm_login()

    def _confirm_login(self):
        """Distinguish 'logged in' from 'credentials rejected'."""
        analytics = self.page.locator("a[data-original-title='Analytics']")
        try:
            analytics.first.wait_for(state="visible", timeout=20000)
        except Exception:
            # Still on the login form, or an error banner is showing: the
            # dialer rejected us. This must never be retried.
            still_login = False
            try:
                still_login = self.page.locator("#password").is_visible()
            except Exception:
                pass
            body = ""
            try:
                body = (self.page.inner_text("body") or "")[:400]
            except Exception:
                pass
            hint = re.search(
                r"(invalid|incorrect|wrong|failed|not\s+found|disabled|blocked)[^\n]{0,80}",
                body, re.I,
            )
            if still_login or hint:
                raise InvalidCredentialsError(
                    "Dialer rejected the login for user "
                    f"'{self.username}' (client {self.client_code})"
                    + (f": {hint.group(0).strip()}" if hint else "")
                )
            raise UiChangedError("post-login Analytics link",
                                 ["a[data-original-title='Analytics']"])
        self.ctx.detail(f"Logged in, url={self.page.url}")
        self.ctx.step("Success", stage=Stage.LOGIN)

    # ------------------------------------------------------------- navigation

    def open_analytics(self):
        """Analytics -> User Session, then wait for the report iframe."""
        with self.ctx.stage_scope(Stage.NAVIGATION):
            analytics = resolve(
                self.page,
                [
                    # Icon-only anchor: data-original-title is its only label.
                    ("css [data-original-title='Analytics']",
                     lambda s: s.locator("a[data-original-title='Analytics']")),
                    ("get_by_role(link, /analytics/i)",
                     lambda s: s.get_by_role("link", name=re.compile("analytics", re.I))),
                ],
                self.ctx, "Analytics menu",
            )
            self.runner.run("open Analytics menu", analytics.click, stage=Stage.NAVIGATION)

            pattern = re.compile(self.REPORT_NAME, re.I)
            report_link = resolve(
                self.page,
                [
                    (f"get_by_role(link, /{self.REPORT_NAME}/i)",
                     lambda s: s.get_by_role("link", name=pattern)),
                    (f"get_by_text(/{self.REPORT_NAME}/i)",
                     lambda s: s.get_by_text(pattern)),
                ],
                self.ctx, f"{self.REPORT_LABEL} report link",
            )
            self.runner.run(f"open {self.REPORT_LABEL} report", report_link.click,
                            stage=Stage.NAVIGATION)

            self.runner.run(
                "wait for report iframe",
                lambda: self.frame.locator("table.header").first.wait_for(
                    state="visible", timeout=30000
                ),
                stage=Stage.NAVIGATION,
            )
            self.ctx.step("Report page ready", stage=Stage.NAVIGATION)

    # -------------------------------------------------------------- dropdowns

    def _sumo(self, select_id, placeholder):
        """The SumoSelect widget wrapping ``select#<select_id>``."""
        return resolve(
            self.frame,
            [
                (f"get_by_role(button, /{placeholder}/i)",
                 lambda s: s.get_by_role("button", name=re.compile(placeholder, re.I))),
                (f"div.SumoSelect:has(select#{select_id})",
                 lambda s: s.locator(f"div.SumoSelect:has(select#{select_id})")),
                (f"nth SumoSelect for #{select_id}",
                 lambda s: s.locator("div.SumoSelect").nth(0 if select_id == "campaign" else 1)),
            ],
            self.ctx, f"{select_id} dropdown",
        )

    def select_all(self, select_id, placeholder):
        """Open a multi-select and make sure every option is ticked."""
        widget = self._sumo(select_id, placeholder)
        self.runner.run(f"open {select_id} dropdown", widget.click, stage=Stage.SCRAPING)

        scope = f"div.SumoSelect:has(select#{select_id})"
        options = self.frame.locator(f"{scope} ul.options")
        options.first.wait_for(state="visible", timeout=10000)

        # The user list is repopulated by load_lead_user() when the campaign
        # selection is applied, so it can still be empty here. Counting the
        # options rather than trusting the widget's class is what makes this
        # deterministic - the 'selected' class is set even while the list is
        # empty, which is why the Selenium version had to click blindly twice.
        items = self.frame.locator(f"{scope} ul.options li")
        total = self._wait_for_options(items, select_id)

        select_all = self.frame.locator(f"{scope} p.select-all").first
        select_all.wait_for(state="visible", timeout=10000)

        chosen = self.frame.locator(f"{scope} ul.options li.selected")
        for attempt in range(1, 4):
            count = chosen.count()
            if count == total and total > 0:
                break
            self.ctx.detail(
                f"{select_id}: {count}/{total} selected, clicking Select All "
                f"(attempt {attempt}, class={select_all.get_attribute('class')!r})"
            )
            select_all.click()
            self.page.wait_for_timeout(300)

        selected = chosen.count()
        if selected == 0:
            raise UiChangedError(
                f"{select_id} dropdown: no options could be selected "
                f"({total} option(s) present)",
                [f"{scope} ul.options li.selected"],
            )
        if selected != total:
            self.ctx.warn(f"{select_id}: only {selected}/{total} option(s) selected")
        self.ctx.detail(f"{select_id}: {selected}/{total} option(s) selected")

        ok = resolve(
            self.frame,
            [
                (f"btnOk in #{select_id}",
                 lambda s: s.locator(f"div.SumoSelect:has(select#{select_id}) p.btnOk")),
                ("get_by_text('OK') in dropdown",
                 lambda s: s.locator(f"div.SumoSelect:has(select#{select_id})")
                            .get_by_text(re.compile(r"^\s*ok\s*$", re.I))),
                ("any p.btnOk", lambda s: s.locator("p.btnOk")),
            ],
            self.ctx, f"{select_id} OK button",
        )
        self.runner.run(f"confirm {select_id} selection", ok.click, stage=Stage.SCRAPING)
        # Returned so the caller can report both dropdowns on one line. Must
        # come after the OK click: confirming the campaign selection is what
        # triggers the user list to load.
        return selected, total

    def _wait_for_options(self, items, select_id, timeout_ms=20000):
        """Wait for the option list to populate; return how many there are."""
        deadline = timeout_ms
        step = 500
        total = items.count()
        while total == 0 and deadline > 0:
            self.page.wait_for_timeout(step)
            deadline -= step
            total = items.count()
        if total == 0:
            raise UiChangedError(
                f"{select_id} dropdown never populated its option list",
                [f"div.SumoSelect:has(select#{select_id}) ul.options li"],
            )
        self.ctx.detail(f"{select_id}: {total} option(s) available")
        return total


    def select_all_campaigns(self):
        """Returns (selected, total) so the caller can report both dropdowns on
        one line instead of two."""
        with self.ctx.stage_scope(Stage.SCRAPING):
            return self.select_all("campaign", "select campaigns")

    def select_all_users(self):
        with self.ctx.stage_scope(Stage.SCRAPING):
            return self.select_all("user", "select users")

    # ---------------------------------------------------------- date selection

    def select_date(self, which, target):
        """Pick *target* in the From/To datepicker, verifying year+month+day.

        The day cell is chosen by its own ``data-year``/``data-month``
        attributes and with ``ui-datepicker-other-month`` excluded, so a "7"
        belonging to the neighbouring month can never be clicked. The header is
        checked before the click and the input value after it.
        """
        field_id = self.DATE_FIELDS[which]
        label = {"from": "From Date", "to": "To Date"}[which]

        with self.ctx.stage_scope(Stage.DATE_SELECTION, announce=False):
            self.ctx.detail(f"{label}: selecting {target.isoformat()}")

            field = resolve(
                self.frame,
                [
                    (f"css #{field_id}", lambda s: s.locator(f"#{field_id}")),
                    ("input.hasDatepicker by position",
                     lambda s: s.locator("input.hasDatepicker").nth(
                         0 if which == "from" else 1)),
                ],
                self.ctx, f"{label} input",
            )
            self.ctx.detail(f"{label}: current value {field.input_value()!r}")
            field.click()

            picker = self.frame.locator("#ui-datepicker-div")
            picker.wait_for(state="visible", timeout=10000)

            self._navigate_to_month(picker, target, label)

            # data-month is 0-based in jQuery UI.
            cell = picker.locator(
                f'td[data-month="{target.month - 1}"][data-year="{target.year}"]'
                f":not(.ui-datepicker-other-month)"
            ).get_by_text(str(target.day), exact=True)

            count = cell.count()
            if count != 1:
                raise UiChangedError(
                    f"{label} day cell for {target.isoformat()} "
                    f"(matched {count} cells, expected 1)",
                    [f'td[data-month={target.month - 1}][data-year={target.year}] '
                     f'with text {target.day}'],
                )
            self.ctx.detail(
                f"{label}: clicking day {target.day} in "
                f"{target.strftime('%B %Y')} (data-month={target.month - 1}, "
                f"data-year={target.year}, other-month cells excluded)"
            )
            cell.first.click()

            after = field.input_value()
            expected = target.isoformat()
            if not after.startswith(expected):
                raise UiChangedError(
                    f"{label} did not accept {expected} (field now {after!r})",
                    [f"#{field_id} value startswith {expected}"],
                )
            self.ctx.detail(f"{label}: verified, field now {after!r}")
            return target

    def _navigate_to_month(self, picker, target, label):
        """Step the picker until its header shows target's month and year."""
        month_span = picker.locator(".ui-datepicker-month")
        year_span = picker.locator(".ui-datepicker-year")

        for step in range(1, 37):
            shown_month = month_span.inner_text().strip()
            shown_year = int(year_span.inner_text().strip())
            shown_index = _month_number(shown_month)
            self.ctx.detail(
                f"{label}: picker showing {shown_month} {shown_year}, "
                f"want {target.strftime('%B')} {target.year}"
            )
            if shown_year == target.year and shown_index == target.month:
                self.ctx.detail(
                    f"{label}: calendar on {shown_month} {shown_year} after {step - 1} step(s)"
                )
                return
            go_back = (shown_year, shown_index) > (target.year, target.month)
            arrow = "prev" if go_back else "next"
            picker.locator(f"a.ui-datepicker-{arrow}").click()
            picker.wait_for(state="visible", timeout=5000)
            self.page.wait_for_timeout(200)

        raise UiChangedError(
            f"{label} calendar would not reach {target.strftime('%B %Y')}",
            ["ui-datepicker-prev/next navigation"],
        )

    # ------------------------------------------------------------- downloading

    def download_report(self, destination):
        """Click Export and save the download to *destination*."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        with self.ctx.stage_scope(Stage.DOWNLOAD):
            export = resolve(
                self.frame,
                [
                    # Icon-only button with an empty title: the id is the label.
                    ("css #create-excel", lambda s: s.locator("#create-excel")),
                    ("get_by_role(button, /excel|export/i)",
                     lambda s: s.get_by_role("button", name=re.compile("excel|export", re.I))),
                ],
                self.ctx, "Export to Excel button",
            )
            from core.config import settings

            self.ctx.detail(
                "Requesting export; waiting up to "
                f"{settings.download_timeout_ms // 1000}s for the download"
            )
            with self.page.expect_download(timeout=settings.download_timeout_ms) as info:
                export.click()
            download = info.value

            suggested = download.suggested_filename
            # Reports differ: User Session serves an HTML table named .xls,
            # Disposition serves a real .xlsx. Keep the served extension so
            # the reader (and pandas) see a consistent file.
            served_suffix = Path(suggested).suffix
            if served_suffix and destination.suffix.lower() != served_suffix.lower():
                destination = destination.with_suffix(served_suffix)
            download.save_as(str(destination))
            self.ctx.step(suggested, stage=Stage.DOWNLOAD)
            self.ctx.detail(f"saved to {self.ctx.relative(destination)}")
            return destination, suggested

    @staticmethod
    def read_export(path):
        """Read an export whatever form the dialer served it in.

        This dialer is inconsistent: User Session returns an HTML table under
        an .xls name, Disposition returns a real .xlsx. Sniffing the first
        bytes beats trusting the extension.
        """
        import pandas as pd

        path = Path(path)
        head = path.open("rb").read(8)
        if head[:2] == b"PK":                                   # zip => xlsx
            return pd.read_excel(path)
        if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":     # OLE2 => legacy xls
            return pd.read_excel(path)
        return pd.read_html(str(path))[0]                       # HTML table

    def validate_download(self, path):
        """Confirm the file is a usable export for this report."""
        path = Path(path)
        with self.ctx.stage_scope(Stage.VALIDATION):
            if not path.exists():
                raise DownloadValidationError(f"Downloaded file is missing: {path}")
            size = path.stat().st_size
            if size < MIN_DOWNLOAD_BYTES:
                raise DownloadValidationError(
                    f"Downloaded file is only {size} bytes (expected at least "
                    f"{MIN_DOWNLOAD_BYTES}); the dialer probably returned an error page"
                )
            try:
                frame = self.read_export(path)
            except Exception as exc:
                raise DownloadValidationError(
                    f"Downloaded file could not be read as a table: {exc}"
                ) from exc
            if frame is None or frame.empty:
                raise DownloadValidationError("Downloaded export contains no rows")
            missing = [c for c in self.REQUIRED_COLUMNS if c not in frame.columns]
            if missing:
                raise DownloadValidationError(
                    f"Export is missing expected column(s): {missing}. "
                    f"Found: {list(frame.columns)[:12]}"
                )
            self.ctx.step(f"{len(frame)} rows | {len(frame.columns)} columns",
                          stage=Stage.VALIDATION)
            self.ctx.detail(f"export size {size} bytes")
            return frame


def _month_number(name):
    """'September' or 'Sep' -> 9."""
    from datetime import datetime

    for fmt in ("%B", "%b"):
        try:
            return datetime.strptime(name, fmt).month
        except ValueError:
            continue
    raise UiChangedError(f"unrecognised month name {name!r} in datepicker header",
                         ["ui-datepicker-month"])


class SmartDialDispositionPage(SmartDialPage):
    """The Analytics -> Disposition Report screen (``disp_all.php``).

    Same login, menu, dropdowns and datepicker as User Session, with three
    differences confirmed against the live dialer: a third multi-select
    (Disposition), a To-date input called ``date2`` rather than ``date``, and
    both an Excel and a CSV export button.
    """

    REPORT_NAME = r"disposition\s*report"
    REPORT_LABEL = "Disposition Report"
    DATE_FIELDS = {"from": "date1", "to": "date2"}
    #: Confirmed against the live export (26 columns).
    REQUIRED_COLUMNS = ("Campaign", "Agent ID", "Disposition", "Date")

    def select_all_dispositions(self):
        with self.ctx.stage_scope(Stage.SCRAPING):
            return self.select_all("disp", "select disposition")
