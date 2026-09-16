"""OneXVoice Agent Performance Report (Playwright).

A different platform from Smart Dial: no client code, no iframe, and a
bootstrap daterangepicker instead of a jQuery datepicker. DMI is the only
process on it today, but it lives here rather than in the script so a second
process on the same dialer reuses it.

Flow:

    login -> Performance -> Agent Performance Report
          -> pick the day in the daterangepicker -> Excel export
"""

import re
from datetime import date as date_cls, datetime
from pathlib import Path

from core.browser import resolve
from core.errors import DownloadValidationError, InvalidCredentialsError, UiChangedError
from core.runlog import Stage

#: Anything smaller than this is an error page, not a report.
MIN_DOWNLOAD_BYTES = 256


class OneXVoicePage:
    """The Agent Performance Report page of a OneXVoice tenant."""

    REPORT_LABEL = "Agent Performance Report"

    #: Columns the export must carry for the processing below to mean anything.
    REQUIRED_COLUMNS = ("Login Duration", "Total Breaks", "Agent Id")

    def __init__(self, session, ctx, runner, base_url, username, password):
        self.session = session
        self.ctx = ctx
        self.runner = runner
        self.base_url = base_url
        self.username = username
        self.password = password

    @property
    def page(self):
        return self.session.page

    # -- login --------------------------------------------------------------

    def login(self):
        """Authenticate. Raises InvalidCredentialsError if the dialer says no."""
        with self.ctx.stage_scope(Stage.LOGIN):
            self.runner.run(
                "open login page",
                lambda: self.page.goto(self.base_url, wait_until="domcontentloaded"),
                stage=Stage.LOGIN,
            )
            self.ctx.detail(f"Login page loaded: {self.page.url}")

            for label, field, value in (("user id", "username", self.username),
                                        ("password", "password", self.password)):
                locator = resolve(
                    self.page,
                    [
                        (f"get_by_label({label})",
                         lambda s, f=field: s.get_by_label(re.compile(f, re.I))),
                        (f"css input[name={field}]",
                         lambda s, f=field: s.locator(f"input[name={f}]")),
                    ],
                    self.ctx, f"{label} field",
                )
                # fill(), not type(): the password carries a leading space and
                # keystroke simulation is where such things get trimmed.
                locator.fill(value)

            submit = resolve(
                self.page,
                [
                    ("get_by_role(button, /log ?in|sign ?in|submit/i)",
                     lambda s: s.get_by_role("button",
                                             name=re.compile(r"log ?in|sign ?in|submit", re.I))),
                    ("css [name=submit]", lambda s: s.locator("[name=submit]")),
                ],
                self.ctx, "sign-in button",
            )
            self.runner.run("submit login", submit.click, stage=Stage.LOGIN)
            self.page.wait_for_load_state("domcontentloaded")
            self._confirm_login()
            self.ctx.step("Success", stage=Stage.LOGIN)

    def _confirm_login(self):
        """The dialer keeps you on /login/ and shows a message when it says no."""
        self.page.wait_for_timeout(1500)
        if "/login" in self.page.url.lower():
            text = ""
            try:
                text = self.page.inner_text("body")[:300].strip().replace("\n", " ")
            except Exception:
                pass
            raise InvalidCredentialsError(
                f"Dialer rejected the login for user {self.username!r}"
                + (f": {text}" if text else "")
            )

    # -- navigation ---------------------------------------------------------

    def open_report(self):
        """Performance -> Agent Performance Report."""
        with self.ctx.stage_scope(Stage.NAVIGATION):
            performance = resolve(
                self.page,
                [
                    ("get_by_role(link, Performance)",
                     lambda s: s.get_by_role("link", name=re.compile(r"^\s*Performance\s*$", re.I))),
                    ("link text contains Performance",
                     lambda s: s.locator("a", has_text=re.compile("Performance", re.I))),
                ],
                self.ctx, "Performance menu",
            )
            self.runner.run("open Performance menu", performance.first.click,
                            stage=Stage.NAVIGATION)
            self.page.wait_for_timeout(1200)

            report = resolve(
                self.page,
                [
                    ("get_by_role(link, Agent Performance Report)",
                     lambda s: s.get_by_role("link", name=re.compile(self.REPORT_LABEL, re.I))),
                    ("link text contains the report name",
                     lambda s: s.locator("a", has_text=re.compile(self.REPORT_LABEL, re.I))),
                ],
                self.ctx, f"{self.REPORT_LABEL} link",
            )
            self.runner.run("open the report", report.first.click, stage=Stage.NAVIGATION)
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_selector("#reportrange", timeout=30000)
            self.ctx.step("Report page ready", stage=Stage.NAVIGATION)

    # -- date selection -----------------------------------------------------

    def select_date(self, target):
        """Set the report to a single day.

        The control is a bootstrap daterangepicker, so it wants both ends of a
        range: clicking a day once sets the start and leaves the picker open.
        Clicking the same day twice makes start == end, which is what a
        one-day report needs.
        """
        if not isinstance(target, date_cls):
            raise TypeError(f"select_date expects a date, got {type(target).__name__}")

        with self.ctx.stage_scope(Stage.DATE_SELECTION):
            opener = resolve(
                self.page,
                [("css #reportrange", lambda s: s.locator("#reportrange"))],
                self.ctx, "date range control",
            )
            self.runner.run("open the date picker", opener.click,
                            stage=Stage.DATE_SELECTION)

            # Two .daterangepicker elements exist and only one is on screen,
            # and which one that is changes as the control re-renders. Ask
            # again at every step rather than holding on to the first answer.
            self._reveal_calendars()
            self._show_month(target)

            for _ in range(2):
                calendar = self._live_picker().locator(".drp-calendar.left")
                day = calendar.locator(
                    f"td.available:not(.off):text-is('{target.day}')"
                )
                if not day.count():
                    raise UiChangedError(
                        f"Day {target.day} is not selectable in the "
                        f"{target.strftime('%B %Y')} calendar"
                    )
                day.first.click()
                self.page.wait_for_timeout(400)

            self._apply(self._live_picker())
            shown = self.page.locator("#reportrange").inner_text().strip()
            self.ctx.step(f"{target.isoformat()} ({shown})", stage=Stage.DATE_SELECTION)

    def _reveal_calendars(self):
        """Show the calendars, which some builds hide behind "Custom Range".

        With the ranges list enabled the picker opens on Today / Yesterday /
        Last 7 Days and keeps both calendars hidden until a custom range is
        asked for. Clicking a day before that silently targets an off-screen
        cell. Builds without a ranges list are already showing the calendars
        and are left alone.
        """
        picker = self._live_picker()
        calendar = picker.locator(".drp-calendar.left")
        if calendar.is_visible():
            return

        custom = picker.locator("li[data-range-key]").filter(
            has_text=re.compile(r"custom", re.I))
        if not custom.count():
            raise UiChangedError(
                "The date picker's calendars are hidden and it offers no "
                "Custom Range entry to reveal them"
            )
        self.ctx.detail("Calendars hidden behind the ranges list; "
                        "choosing Custom Range")
        custom.first.click()
        self.page.wait_for_timeout(500)
        self._live_picker().locator(".drp-calendar.left").wait_for(
            state="visible", timeout=10000)

    def _show_month(self, target):
        """Bring the left calendar to target's month and year.

        Two builds of the same control: Agent Performance renders the month
        and year as dropdowns (showDropdowns), while Disposition Analysis
        renders a plain "Sep 2026" heading with prev/next arrows. Use the
        dropdowns when they exist and step with the arrows when they do not,
        rather than assuming either.
        """
        calendar = self._live_picker().locator(".drp-calendar.left")
        month_select = calendar.locator(".monthselect")
        if month_select.count():
            month_select.select_option(label=target.strftime("%b"))
            calendar.locator(".yearselect").select_option(label=str(target.year))
            self.page.wait_for_timeout(300)
            return

        want = (target.year, target.month)
        for _ in range(36):
            picker = self._live_picker()
            calendar = picker.locator(".drp-calendar.left")
            heading = calendar.locator("th.month").inner_text().strip()
            shown = self._parse_month_heading(heading)
            if shown == want:
                return
            arrow = "prev" if shown > want else "next"
            control = picker.locator(f"th.{arrow}.available")
            if not control.count():
                raise UiChangedError(
                    f"Calendar is on {heading} and cannot step {arrow} "
                    f"towards {target.strftime('%B %Y')}"
                )
            control.first.click()
            self.page.wait_for_timeout(300)

        raise UiChangedError(
            f"Calendar would not reach {target.strftime('%B %Y')}"
        )

    @staticmethod
    def _parse_month_heading(heading):
        """'Sep 2026' or 'September 2026' -> (2026, 9)."""
        for fmt in ("%b %Y", "%B %Y"):
            try:
                shown = datetime.strptime(heading, fmt)
            except ValueError:
                continue
            return (shown.year, shown.month)
        raise UiChangedError(f"Unreadable calendar heading: {heading!r}")

    def _live_picker(self):
        """The visible daterangepicker, not the stale hidden twin."""
        pickers = self.page.locator(".daterangepicker")
        for index in range(pickers.count()):
            candidate = pickers.nth(index)
            if candidate.is_visible():
                return candidate
        raise UiChangedError(
            f"No visible date picker after clicking #reportrange "
            f"({pickers.count()} .daterangepicker element(s) in the page)"
        )

    def _apply(self, picker):
        """Commit the range.

        The hidden twin's Apply is permanently disabled, so a bare .applyBtn
        selector matches an element that can never be clicked.
        """
        apply_button = picker.locator(".applyBtn")
        if apply_button.count() and apply_button.first.is_enabled():
            self.runner.run("apply the date range", apply_button.first.click,
                            stage=Stage.DATE_SELECTION)
            self.page.wait_for_timeout(1500)
        else:
            self.ctx.detail("Apply is not enabled; the range is already applied")

    # -- export -------------------------------------------------------------

    def download_report(self, destination):
        """Click Excel and save the download to *destination*."""
        from core.config import settings

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        with self.ctx.stage_scope(Stage.DOWNLOAD):
            export = resolve(
                self.page,
                [
                    ("get_by_role(button, /excel/i)",
                     lambda s: s.get_by_role("button", name=re.compile("excel", re.I))),
                    ("css #excelData", lambda s: s.locator("#excelData")),
                ],
                self.ctx, "Excel export button",
            )
            export.scroll_into_view_if_needed()
            self.ctx.detail(
                "Requesting export; waiting up to "
                f"{settings.download_timeout_ms // 1000}s for the download"
            )
            with self.page.expect_download(timeout=settings.download_timeout_ms) as info:
                # The report's sticky table header overlaps this button, so a
                # plain click lands on a TH. force skips the hit test.
                export.click(force=True)
            download = info.value

            suggested = download.suggested_filename
            served = Path(suggested).suffix
            if served and destination.suffix.lower() != served.lower():
                destination = destination.with_suffix(served)
            download.save_as(str(destination))
            self.ctx.step(suggested, stage=Stage.DOWNLOAD)
            self.ctx.detail(f"saved to {self.ctx.relative(destination)}")
            return destination, suggested

    @staticmethod
    def read_export(path, dtype=None):
        """Read the export whatever form it was served in.

        Pass ``dtype=str`` when the values matter as text - phone numbers and
        agent ids must not be coerced into floats and come back as 9.1234e+09.
        """
        import pandas as pd

        path = Path(path)
        head = path.open("rb").read(8)
        if head[:2] == b"PK":                                   # zip => xlsx
            return pd.read_excel(path, dtype=dtype)
        if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":     # OLE2 => legacy xls
            return pd.read_excel(path, dtype=dtype)
        try:
            return pd.read_html(str(path))[0]                   # HTML table
        except ValueError:
            return pd.read_csv(path, dtype=dtype)               # plain CSV

    def validate_download(self, path):
        """Confirm the file is a usable Agent Performance export."""
        path = Path(path)
        with self.ctx.stage_scope(Stage.VALIDATION):
            if not path.exists():
                raise DownloadValidationError(f"Downloaded file is missing: {path}")
            size = path.stat().st_size
            if size < MIN_DOWNLOAD_BYTES:
                raise DownloadValidationError(
                    f"Downloaded file is only {size} bytes; the dialer probably "
                    "returned an error page rather than a report"
                )
            try:
                frame = self.read_export(path)
            except Exception as exc:
                raise DownloadValidationError(
                    f"Downloaded file could not be read as a table: {exc}"
                ) from exc
            if frame is None or frame.empty:
                raise DownloadValidationError("The export contains no rows")

            missing = [c for c in self.REQUIRED_COLUMNS if c not in frame.columns]
            if missing:
                raise DownloadValidationError(
                    f"Export is missing expected column(s): {', '.join(missing)}; "
                    f"got {list(frame.columns)[:12]}"
                )
            self.ctx.step(f"{len(frame)} rows | {len(frame.columns)} columns",
                          stage=Stage.VALIDATION)
            return frame


class OneXVoiceDispositionPage(OneXVoicePage):
    """The Performance -> Disposition Analysis page of a OneXVoice tenant.

    This is what the vKYC dialer offers in place of Smart Dial's Disposition
    Report, and it is not the same shape: Smart Dial lists one row per call,
    while this lists one row per disposition combination with a count. The
    dialer serves no per-call export carrying dispositions - Call Analysis is
    a daily total and the Call Log grid does not export them - so this is the
    disposition data available for this tenant.
    """

    REPORT_LABEL = "Disposition Analysis"

    #: The three disposition levels. The count arrives in a fourth, unnamed
    #: column, which read_export renames rather than requiring by name.
    REQUIRED_COLUMNS = ("Level 1", "Level 2", "Level 3")

    #: What the unnamed count column is called once read.
    COUNT_COLUMN = "Count"

    @classmethod
    def read_export(cls, path, dtype=None):
        """Read the export and give its unnamed count column a name."""
        frame = OneXVoicePage.read_export(path, dtype=dtype)
        renamed = {c: cls.COUNT_COLUMN for c in frame.columns
                   if isinstance(c, str) and c.startswith("Unnamed:")}
        return frame.rename(columns=renamed) if renamed else frame
