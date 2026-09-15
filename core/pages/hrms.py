"""Page object for the HRMS "Upload Productive Hour(CSV)" screen.

The commit is the *Save Excel* click; *Import Data* only validates and fills
the ``GV_Error`` grid, which is why :meth:`import_data` can be run without
writing anything. The grid lists one row per record with an ``ErrorMessage``
column that is blank when the row is fine, so a rejected agent is a row whose
first cell has text - not merely a row that exists.
"""

import re

from core.browser import resolve
from core.errors import InvalidCredentialsError, UiChangedError
from core.runlog import Stage


class HrmsPage:
    """Drives one HRMS upload session."""

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

    @property
    def frame(self):
        """The application iframe every screen lives in."""
        return self.page.frame_locator("#mainFrame1")

    # ------------------------------------------------------------------ login

    def login(self):
        with self.ctx.stage_scope(Stage.LOGIN):
            self.runner.run(
                "open HRMS login page",
                lambda: self.page.goto(self.base_url, wait_until="domcontentloaded"),
                stage=Stage.LOGIN,
            )
            for label, fallback_id, value in (
                ("user name", "#txtUserName", self.username),
                ("password", "#txtPasswd", self.password),
            ):
                field = resolve(
                    self.page,
                    [
                        # HRMS labels its login inputs with placeholders, not
                        # <label>, so that is the preferred strategy here.
                        (f"get_by_placeholder(/{label}/i)",
                         lambda s, l=label: s.get_by_placeholder(
                             re.compile(l.replace(" ", r"\s*"), re.I))),
                        (f"get_by_label(/{label}/i)",
                         lambda s, l=label: s.get_by_label(re.compile(l.replace(" ", r"\s*"), re.I))),
                        (f"css {fallback_id}", lambda s, f=fallback_id: s.locator(f)),
                    ],
                    self.ctx, f"HRMS login field '{label}'",
                )
                field.fill(value)
                self.ctx.detail(f"filled HRMS login field '{label}'")

            submit = resolve(
                self.page,
                [
                    ("get_by_role(button, /login|sign in|submit/i)",
                     lambda s: s.get_by_role(
                         "button", name=re.compile(r"log\s*in|sign\s*in|submit", re.I))),
                    ("css #btnSubmit", lambda s: s.locator("#btnSubmit")),
                ],
                self.ctx, "HRMS login submit",
            )
            submit.click()
            self._confirm_login()

    def _confirm_login(self):
        try:
            self.page.locator("#mainFrame1").wait_for(state="attached", timeout=20000)
        except Exception:
            body = ""
            try:
                body = (self.page.inner_text("body") or "")[:400]
            except Exception:
                pass
            if re.search(r"invalid|incorrect|failed|wrong", body, re.I) or \
                    self.page.locator("#txtPasswd").is_visible():
                raise InvalidCredentialsError(
                    f"HRMS rejected the login for user '{self.username}'"
                )
            raise UiChangedError("HRMS application frame", ["#mainFrame1"])
        self.ctx.detail(f"logged in, url={self.page.url}")
        self.ctx.step("Success", stage=Stage.LOGIN)

    # ------------------------------------------------------------- navigation

    def open_upload_page(self):
        """Leave Section -> Upload Productive Hour(CSV)."""
        with self.ctx.stage_scope(Stage.NAVIGATION):
            menu = resolve(
                self.frame,
                [
                    ("get_by_role(link, 'Leave Section')",
                     lambda s: s.get_by_role("link", name=re.compile(r"leave\s*section", re.I))),
                    ("get_by_text('Leave Section')",
                     lambda s: s.get_by_text(re.compile(r"leave\s*section", re.I))),
                ],
                self.ctx, "Leave Section menu",
            )
            menu.hover()

            item = resolve(
                self.frame,
                [
                    ("get_by_role(link, /upload productive hour/i)",
                     lambda s: s.get_by_role(
                         "link", name=re.compile(r"upload\s*productive\s*hour", re.I))),
                    ("get_by_text(/upload productive hour/i)",
                     lambda s: s.get_by_text(re.compile(r"upload\s*productive\s*hour", re.I))),
                ],
                self.ctx, "Upload Productive Hour(CSV) menu item",
            )
            self.runner.run("open upload screen", item.click, stage=Stage.NAVIGATION)

            self.runner.run(
                "wait for upload form",
                lambda: self._file_input().wait_for(state="attached", timeout=30000),
                stage=Stage.NAVIGATION,
            )
            self.ctx.step("Upload page ready", stage=Stage.NAVIGATION)

    def _file_input(self):
        return resolve(
            self.frame,
            [
                ("input[type=file]", lambda s: s.locator("input[type='file']")),
                ("css #FileUpload1", lambda s: s.locator("#FileUpload1")),
            ],
            self.ctx, "CSV file input", state="attached",
        )

    # ----------------------------------------------------------------- upload

    def attach(self, file_path):
        with self.ctx.stage_scope(Stage.HRMS_UPLOAD):
            self._file_input().set_input_files(str(file_path))
            self.ctx.detail(f"attached {self.ctx.relative(file_path)}")

    def import_data(self):
        """Click Import Data (validation only) and return the rejected rows."""
        with self.ctx.stage_scope(Stage.HRMS_UPLOAD, announce=False):
            button = resolve(
                self.frame,
                [
                    ("get_by_role(button, /import data/i)",
                     lambda s: s.get_by_role("button", name=re.compile(r"import\s*data", re.I))),
                    ("css #btn_ImpData", lambda s: s.locator("#btn_ImpData")),
                ],
                self.ctx, "Import Data button",
            )
            self.runner.run("import data", button.click, stage=Stage.HRMS_UPLOAD)

            grid = self.frame.locator("table#GV_Error")
            try:
                grid.wait_for(state="visible", timeout=30000)
            except Exception:
                self.ctx.warn("No validation grid appeared after import")
                return []

            rejected = []
            rows = grid.locator("tr")
            total = rows.count()
            for index in range(total):
                cells = rows.nth(index).locator("td")
                if cells.count() == 0:
                    continue  # header row uses <th>
                values = [cells.nth(i).inner_text().strip() for i in range(cells.count())]
                # First column is ErrorMessage: blank means the row is fine.
                if values and values[0]:
                    rejected.append(values)

            self.ctx.step(f"Validated: {total} rows | {len(rejected)} rejected",
                          stage=Stage.HRMS_UPLOAD)
            return rejected

    def save(self):
        """Commit the upload. This writes to HRMS."""
        with self.ctx.stage_scope(Stage.HRMS_UPLOAD, announce=False):
            button = resolve(
                self.frame,
                [
                    ("get_by_role(button, /save excel/i)",
                     lambda s: s.get_by_role("button", name=re.compile(r"save\s*excel", re.I))),
                    ("input[value='Save Excel']",
                     lambda s: s.locator("input[value='Save Excel']")),
                ],
                self.ctx, "Save Excel button",
            )
            self.runner.run("save upload", button.click, stage=Stage.HRMS_UPLOAD)
            self.ctx.detail("Save Excel clicked")

    def confirm_saved(self, timeout_ms=20000):
        """Return the success banner text, or None if it never appeared."""
        with self.ctx.stage_scope(Stage.VALIDATION, announce=False):
            banner = self.frame.locator(
                "//div[@id='MessageBar_MessageBox' and contains(@class,'success')]//p"
            )
            try:
                banner.first.wait_for(state="visible", timeout=timeout_ms)
            except Exception:
                self.ctx.warn("No success message shown after save")
                return None
            text = banner.first.inner_text().strip()
            self.ctx.step(text, stage=Stage.HRMS_UPLOAD)
            return text
