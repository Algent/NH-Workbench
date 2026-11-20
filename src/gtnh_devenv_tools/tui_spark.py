"""The spark tool of the Tools tab: a spark profile as a text report you can drill into."""

import asyncio
import re
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from . import spark
from .config import cache_dir
from .tui import MenuScreen, PromptScreen

_LEAD = re.compile(r"^\s*[\d.]+%\s+(?:\d+h\d\dm|\d+m\d\ds|[\d.]+s|\d+ [MG]B)?\s*")
_OWN = re.compile(r"\s+\(own [\d.]+%\)$")
_WINDOW_ROW = re.compile(r"^(\d+)\s+(?:\d+h\d\dm|\d+m\d\ds|[\d.]+s|\?)\s+\d+\s")
_METHOD = re.compile(r"^[\w$.<>/-]+$")
RECENT = 20  # saved profiles listed


def method_on_line(line: str) -> str | None:
    """The method (or package) a report line is about: the last frame of a call path, or the listed name."""
    if not _LEAD.match(line):
        return None
    rest = _OWN.sub("", _LEAD.sub("", line, count=1)).strip()
    last = rest.rpartition(" > ")[2].strip()
    return last if _METHOD.match(last) else None


def window_on_line(line: str) -> int | None:
    m = _WINDOW_ROW.match(line)
    return int(m[1]) if m else None


def saved_profiles() -> list[Path]:
    try:
        files = list((cache_dir() / "spark").glob("*.sparkprofile"))
    except OSError:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:RECENT]


class SparkScreen(Screen[None]):
    """Load a profile, read its summary, then follow methods into call trees with Enter."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", show=False),
        Binding("enter", "dig", "Dig in", show=False),
        Binding("o", "open", "Open other"),
        Binding("s", "summary", "Summary"),
        Binding("t", "tree(False)", "Calls"),
        Binding("c", "tree(True)", "Callers"),
        Binding("w", "window", "Minute"),
        Binding("h", "thread", "Thread"),
        Binding("y", "copy", "Copy"),
        Binding("e", "save", "Save"),
        Binding("r", "reload", "Download again"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.profile: spark.Profile | None = None
        self.name_: str = ""
        self.source: str = ""
        self.thread: str | None = None
        self.windows: list[int] | None = None
        self.method: str | None = None  # None: the summary
        self.up = False  # callers instead of callees
        self.text = ""

    def compose(self) -> ComposeResult:
        yield Static(Text("spark profile", "bold"), id="spark-status")
        yield Input(placeholder="spark.lucko.me link, its code, or a .sparkprofile file, then Enter", id="spark-input")
        yield OptionList(id="spark-recent")
        report = TextArea(id="spark-report", read_only=True, soft_wrap=False, show_line_numbers=False)
        report.display = False
        yield report
        yield Static(
            Text(
                "Enter on a method: what it calls (t), c: who calls it, Enter on a minute: only that minute,"
                " s: summary, y: copy for an LLM",
                "dim",
            ),
            id="spark-hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._fill_recent()
        self.query_one("#spark-input", Input).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("open", "back"):
            return True
        return self.profile is not None

    # --- loading -----------------------------------------------------------------

    def _fill_recent(self) -> None:
        recent = self.query_one("#spark-recent", OptionList)
        recent.clear_options()
        files = saved_profiles()
        for path in files:
            when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            recent.add_option(Option(Text.assemble((path.stem, "bold"), f"   downloaded {when}"), id=str(path)))
        recent.display = bool(files)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.value.strip():
            self.load(event.value.strip())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.load(event.option.id or "")

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()  # keep it away from the app's filter box

    @work(group="spark", exclusive=True)
    async def load(self, source: str, refresh: bool = False) -> None:
        self.notify("Loading the profile...")
        try:
            profile, name = await asyncio.to_thread(spark.load, source, refresh)
        except spark.ProfileError as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        self.profile, self.name_, self.source = profile, name, source
        self.thread = self.windows = self.method = None
        self.query_one("#spark-input", Input).display = False
        self.query_one("#spark-recent", OptionList).display = False
        report = self.query_one("#spark-report", TextArea)
        report.display = True
        report.focus()
        self.refresh_bindings()
        await self._show()

    async def _show(self) -> None:
        p = self.profile
        if p is None:
            return
        try:
            if self.method is None:
                text = await asyncio.to_thread(spark.summary, p, self.name_, self.thread, self.windows)
            else:
                text = await asyncio.to_thread(
                    spark.focus_report, p, self.name_, self.method, self.up, self.thread, self.windows
                )
        except spark.ProfileError as e:
            self.notify(str(e), severity="warning")
            return
        self.text = text
        report = self.query_one("#spark-report", TextArea)
        report.load_text(text)
        report.move_cursor((0, 0))
        where = [self.name_, self.thread or "server thread"]
        if self.windows:
            where.append(f"minute {', '.join(map(str, self.windows))}")
        view = "summary" if self.method is None else f"{'callers of' if self.up else 'calls of'} {self.method}"
        self.query_one("#spark-status", Static).update(
            Text.assemble(("spark ", "bold"), " · ".join(where), ("  ·  ", "dim"), (view, "bold"))
        )

    def _cursor_line(self) -> str:
        report = self.query_one("#spark-report", TextArea)
        return report.document.get_line(report.cursor_location[0])

    # --- actions -------------------------------------------------------------------

    def action_back(self) -> None:
        box = self.query_one("#spark-input", Input)
        if box.display and self.profile is not None:  # leave "open other" without loading anything
            box.display = False
            self.query_one("#spark-recent", OptionList).display = False
            self.query_one("#spark-report", TextArea).focus()
            return
        self.dismiss()

    def action_open(self) -> None:
        box = self.query_one("#spark-input", Input)
        box.display = True
        box.value = ""
        self._fill_recent()
        box.focus()

    async def action_dig(self) -> None:
        line = self._cursor_line()
        window = window_on_line(line)
        if window is not None:
            self.windows = None if self.windows == [window] else [window]
            await self._show()
            return
        method = method_on_line(line)
        if method:
            self.method, self.up = method, False
            await self._show()

    async def action_summary(self) -> None:
        self.method = None
        await self._show()

    @work(group="spark-dialog")
    async def action_tree(self, up: bool) -> None:
        method = method_on_line(self._cursor_line())
        if not method:
            hint = "Part of Class.method, any case. Tip: put the cursor on a line of the report instead."
            method = await self.app.push_screen_wait(PromptScreen("Method", self.method or "", hint))
        if method:
            self.method, self.up = method, up
            await self._show()

    @work(group="spark-dialog")
    async def action_window(self) -> None:
        p = self.profile
        if p is None:
            return
        worst = sorted((w for w in p.windows if w.ticks), key=lambda w: -w.mspt_max)[:9]
        actions = [("a", "all", "Whole recording", None if self.windows else "shown now")]
        for i, w in enumerate(worst, 1):
            at = spark._span(w.start - p.start) if w.start and p.start else "?"
            label = f"{w.id} at {at}: slowest tick {w.mspt_max:.0f} ms, TPS {w.tps:.1f}"
            actions.append((str(i), str(w.id), label, None))
        title = Text("Only one minute (worst first)", "bold")
        choice = await self.app.push_screen_wait(MenuScreen(title, actions))
        if choice:
            self.windows = None if choice == "all" else [int(choice)]
            await self._show()

    @work(group="spark-dialog")
    async def action_thread(self) -> None:
        p = self.profile
        if p is None:
            return
        threads = spark.busiest_threads(p)[:9]
        actions = [(str(i), t.name, t.name, None) for i, t in enumerate(threads, 1)]
        choice = await self.app.push_screen_wait(MenuScreen(Text("Thread", "bold"), actions))
        if choice:
            self.thread = choice
            await self._show()

    def action_copy(self) -> None:
        if self.text:
            self.app.copy_to_clipboard(self.text)
            self.notify(
                f"Copied {self.text.count(chr(10)) + 1} lines. Nothing in the clipboard? Your terminal blocks it: "
                "e saves a file instead."
            )

    def action_save(self) -> None:
        if not self.text:
            return
        view = "summary" if self.method is None else f"{'callers' if self.up else 'calls'}-{self.method}"
        safe = re.sub(r"[^\w.-]+", "_", f"{self.name_}-{view}")[:120]
        path = cache_dir() / "spark" / f"{safe}.md"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.text, encoding="utf-8")
        except OSError as e:
            self.notify(f"Could not save: {e}", severity="error")
            return
        self.notify(f"Saved to {path}", timeout=10)

    def action_reload(self) -> None:
        if self.source:
            self.load(self.source, refresh=True)
