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
from datetime import date as date_cls
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

            # Two .daterangepicker elements exist; only one is on screen.
            picker = self._live_picker()
            calendar = picker.locator(".drp-calendar.left")

            calendar.locator(".monthselect").select_option(label=target.strftime("%b"))
            calendar.locator(".yearselect").select_option(label=str(target.year))
            self.page.wait_for_timeout(300)

            # Re-locate between clicks: the picker re-renders after the first.
            for _ in range(2):
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

            self._apply(picker)
            shown = self.page.locator("#reportrange").inner_text().strip()
            self.ctx.step(f"{target.isoformat()} ({shown})", stage=Stage.DATE_SELECTION)

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
    def read_export(path):
        """Read the export whatever form it was served in."""
        import pandas as pd

        path = Path(path)
        head = path.open("rb").read(8)
        if head[:2] == b"PK":                                   # zip => xlsx
            return pd.read_excel(path)
        if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":     # OLE2 => legacy xls
            return pd.read_excel(path)
        try:
            return pd.read_html(str(path))[0]                   # HTML table
        except ValueError:
            return pd.read_csv(path)                            # plain CSV

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
