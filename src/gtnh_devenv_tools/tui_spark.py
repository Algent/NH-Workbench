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
_TAG = re.compile(r"^\[[^\]]*\]\s*")  # [Mod] in front of a frame
RECENT = 20  # saved profiles listed
INSTALL = "install-skill"  # id of the list entry that installs the skill
HINTS = {
    "pick": "Paste a link or code and press Enter, or pick a saved profile. F2: install the AI skill.",
    "read": "Enter on a method: what it calls (t), c: who calls it, Enter on a minute: only that minute,"
    " Esc: step back, s: summary, y: copy for an LLM",
}


def method_on_line(line: str) -> str | None:
    """The method (or package) a report line is about: the last frame of a call path, or the listed name."""
    if not _LEAD.match(line):
        return None
    rest = _OWN.sub("", _LEAD.sub("", line, count=1)).strip()
    last = _TAG.sub("", rest.rpartition(" > ")[2].strip().removeprefix("< "))
    return last if _METHOD.match(last) else None


def window_on_line(line: str) -> int | None:
    m = _WINDOW_ROW.match(line)
    return int(m[1]) if m else None


def saved_profiles() -> list[Path]:
    try:
        folder = cache_dir() / "spark"
        files = [*folder.glob("*.sparkprofile"), *folder.glob("*.sparkheap")]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:RECENT]


class SparkScreen(Screen[None]):
    """Load a profile, read its summary, then follow methods into call trees with Enter."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "leave", "Close"),
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
        Binding("m", "compare", "Compare later"),
        Binding("f2", "install_skill", "Install AI skill"),  # a letter would go into the text box
    ]

    def __init__(self) -> None:
        super().__init__()
        self.profile: spark.Profile | None = None
        self.heap: spark.Heap | None = None  # a heap summary instead of a profile
        self.other: tuple[spark.Heap, str] | None = None  # a later heap summary to compare with
        self.name_: str = ""
        self.source: str = ""
        self.thread: str | None = None
        self.windows: list[int] | None = None
        self.method: str | None = None  # None: the summary
        self.up = False  # callers instead of callees
        self.text = ""
        self.history: list[tuple] = []  # earlier views, for Esc: (method, up, windows, thread, other, cursor row)

    def compose(self) -> ComposeResult:
        yield Static(Text("spark profile", "bold"), id="spark-status")
        hint = "spark.lucko.me link (profile or heap summary), its code, or a downloaded file, then Enter"
        yield Input(placeholder=hint, id="spark-input")
        yield OptionList(id="spark-recent")
        report = TextArea(id="spark-report", read_only=True, soft_wrap=False, show_line_numbers=False)
        report.display = False
        yield report
        yield Static(Text(HINTS["pick"], "dim"), id="spark-hint")
        yield Footer()

    def on_mount(self) -> None:
        self._fill_recent()
        self.query_one("#spark-input", Input).focus()

    def _picking(self) -> bool:
        """The text box and saved profiles are shown (nothing loaded yet, or "open other")."""
        return bool(self.query_one("#spark-input", Input).display)

    def _set_mode(self, picking: bool) -> None:
        self.query_one("#spark-input", Input).display = picking
        self.query_one("#spark-recent", OptionList).display = picking
        self.query_one("#spark-report", TextArea).display = not picking and self._loaded()
        self.query_one("#spark-hint", Static).update(Text(HINTS["pick" if picking else "read"], "dim"))
        self.refresh_bindings()

    def _loaded(self) -> bool:
        return self.profile is not None or self.heap is not None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "back":
            return True
        if self._picking():
            return action == "install_skill"
        if action == "install_skill":
            return False
        if action in ("open", "leave"):
            return True
        if self.heap is not None:  # no call tree, minutes or threads in a heap summary
            return action in ("summary", "copy", "save", "reload", "compare")
        return action != "compare" and self.profile is not None

    # --- loading -----------------------------------------------------------------

    def _fill_recent(self) -> None:
        recent = self.query_one("#spark-recent", OptionList)
        recent.clear_options()
        files = saved_profiles()
        for path in files:
            when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            kind = "heap summary" if path.suffix == ".sparkheap" else "profile"
            label = Text.assemble((path.stem, "bold"), f"   {kind}, downloaded {when}")
            recent.add_option(Option(label, id=str(path)))
        if files:
            recent.add_option(None)  # separator
        label = Text.assemble(("Install the AI skill", "bold"), "   for Claude Code or Codex (F2)")
        recent.add_option(Option(label, id=INSTALL))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.value.strip():
            self.load(event.value.strip())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id == INSTALL:
            self.action_install_skill()
        else:
            self.load(event.option.id or "")

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()  # keep it away from the app's filter box

    @work(group="spark", exclusive=True)
    async def load(self, source: str, refresh: bool = False) -> None:
        self.notify("Loading...")
        try:
            upload, name = await asyncio.to_thread(spark.load, source, refresh)
        except spark.ProfileError as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        if isinstance(upload, spark.Heap):
            self.heap, self.profile = upload, None
        else:
            self.profile, self.heap = upload, None
        self.name_, self.source = name, source
        self.thread = self.windows = self.method = self.other = None
        self.history = []
        self._set_mode(picking=False)
        self.query_one("#spark-report", TextArea).focus()
        await self._show()

    async def _show(self, row: int = 0) -> bool:
        """Render the current view, the cursor on the given row. False when it cannot be shown."""
        if self.heap is not None:
            return await self._show_heap(row)
        p = self.profile
        if p is None:
            return False
        try:
            if self.method is None:
                text = await asyncio.to_thread(spark.summary, p, self.name_, self.thread, self.windows)
            else:
                text = await asyncio.to_thread(
                    spark.focus_report, p, self.name_, self.method, self.up, self.thread, self.windows
                )
        except spark.ProfileError as e:
            self.notify(str(e), severity="warning")
            return False
        self._put_text(text, row)
        where = [self.name_, self.thread or "server thread"]
        if self.windows:
            where.append(f"minute {', '.join(map(str, self.windows))}")
        view = "summary" if self.method is None else f"{'callers of' if self.up else 'calls of'} {self.method}"
        self.query_one("#spark-status", Static).update(
            Text.assemble(("spark ", "bold"), " · ".join(where), ("  ·  ", "dim"), (view, "bold"))
        )
        return True

    async def _show_heap(self, row: int) -> bool:
        h = self.heap
        if self.other:
            text = await asyncio.to_thread(spark.heap_compare, h, self.name_, self.other[0], self.other[1])
            view = f"growth until {self.other[1]}"
        else:
            text = await asyncio.to_thread(spark.heap_report, h, self.name_)
            view = "heap summary (m: compare with a later one)"
        self._put_text(text, row)
        self.query_one("#spark-status", Static).update(
            Text.assemble(("spark ", "bold"), self.name_, ("  ·  ", "dim"), (view, "bold"))
        )
        return True

    def _put_text(self, text: str, row: int) -> None:
        self.text = text
        report = self.query_one("#spark-report", TextArea)
        report.load_text(text)
        report.move_cursor((min(row, report.document.line_count - 1), 0), center=bool(row))

    def _snapshot(self) -> tuple:
        report = self.query_one("#spark-report", TextArea)
        return (self.method, self.up, self.windows, self.thread, self.other, report.cursor_location[0])

    async def _go(self, **changes: object) -> None:
        """Switch to another view, remembering this one for Esc."""
        before = self._snapshot()
        for key, value in changes.items():
            setattr(self, key, value)
        if await self._show():
            self.history.append(before)
        else:
            self.method, self.up, self.windows, self.thread, self.other, _ = before

    def _cursor_line(self) -> str:
        report = self.query_one("#spark-report", TextArea)
        return report.document.get_line(report.cursor_location[0])

    # --- actions -------------------------------------------------------------------

    async def action_back(self) -> None:
        """Close "open other", else step back to the previous view, else leave the screen."""
        box = self.query_one("#spark-input", Input)
        if box.display and self._loaded():  # leave "open other" without loading anything
            self._set_mode(picking=False)
            self.query_one("#spark-report", TextArea).focus()
            return
        if self.history:
            self.method, self.up, self.windows, self.thread, self.other, row = self.history.pop()
            await self._show(row)
            return
        self.dismiss()

    def action_leave(self) -> None:
        self.dismiss()

    def action_open(self) -> None:
        box = self.query_one("#spark-input", Input)
        box.value = ""
        self._fill_recent()
        self._set_mode(picking=True)
        box.focus()

    async def action_dig(self) -> None:
        if self.heap is not None:
            return
        line = self._cursor_line()
        window = window_on_line(line)
        if window is not None:
            await self._go(windows=None if self.windows == [window] else [window])
            return
        method = method_on_line(line)
        if method:
            await self._go(method=method, up=False)

    async def action_summary(self) -> None:
        """Straight back to the summary (minute and thread kept); Esc then leaves."""
        self.history.clear()
        self.method = self.other = None
        await self._show()

    @work(group="spark-dialog")
    async def action_tree(self, up: bool) -> None:
        method = method_on_line(self._cursor_line())
        if not method:
            hint = "Part of Class.method, any case. Tip: put the cursor on a line of the report instead."
            method = await self.app.push_screen_wait(PromptScreen("Method", self.method or "", hint))
        if method:
            await self._go(method=method, up=up)

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
            await self._go(windows=None if choice == "all" else [int(choice)])

    @work(group="spark-dialog")
    async def action_thread(self) -> None:
        p = self.profile
        if p is None:
            return
        threads = spark.busiest_threads(p)[:9]
        actions = [(str(i), t.name, t.name, None) for i, t in enumerate(threads, 1)]
        choice = await self.app.push_screen_wait(MenuScreen(Text("Thread", "bold"), actions))
        if choice:
            await self._go(thread=choice)

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

    @work(group="spark-dialog")
    async def action_install_skill(self) -> None:
        """Install the skill that lets Claude Code or Codex read spark links with this tool."""
        await self.app.install_skill_menu()

    @work(group="spark-dialog")
    async def action_compare(self) -> None:
        """Compare this heap summary with a later one: what grew is what leaks."""
        if self.heap is None:
            return
        hint = "The later heap summary: link, code or file. Saved ones are listed when you press o."
        source = await self.app.push_screen_wait(PromptScreen("Compare with a later heap summary", "", hint))
        if not source:
            return
        try:
            other = await asyncio.to_thread(spark.load_heap, source)
        except spark.ProfileError as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        await self._go(other=other)

    def action_reload(self) -> None:
        if self.source:
            self.load(self.source, refresh=True)
