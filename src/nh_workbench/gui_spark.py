"""The spark tool of the desktop UI's Tools tab: a spark profile as a text report you can drill into.

The desktop counterpart of tui_spark.SparkScreen, on the same spark module and the same report line parsing.
"""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import spark
from .tui_spark import method_on_line, saved_profiles, window_on_line

if TYPE_CHECKING:
    from .gui import MainWindow

HINT = (
    "Enter or double click on a method: what it calls (t)   c: who calls it   Enter on a minute: only that minute"
    "   Esc: step back   s: summary   y: copy for an LLM"
)


class Report(QPlainTextEdit):
    """The report, read only but with a keyboard cursor; keys and double clicks go to `on_key`."""

    def __init__(self, on_key: Callable[[int, str], bool]) -> None:
        super().__init__()
        self.on_key = on_key
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        flags = Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        self.setTextInteractionFlags(flags)

    def keyPressEvent(self, event) -> None:
        plain = event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)
        if plain and self.on_key(event.key(), event.text().lower()):
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        super().mouseDoubleClickEvent(event)
        self.on_key(Qt.Key.Key_Return, "")

    def line(self) -> str:
        return self.textCursor().block().text()

    def row(self) -> int:
        return self.textCursor().blockNumber()

    def show_text(self, text: str, row: int) -> None:
        self.setPlainText(text)
        block = self.document().findBlockByNumber(min(row, self.document().blockCount() - 1))
        self.setTextCursor(QTextCursor(block))
        if row:
            self.centerCursor()


class SparkWindow(QWidget):
    """Load a profile, read its summary, then follow methods into call trees with Enter."""

    def __init__(self, owner: "MainWindow") -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.owner = owner  # its asyncio loop runs the downloads and reports
        self.setWindowTitle("spark profiles - NH Workbench")
        self.setWindowIcon(owner.windowIcon())
        self.resize(1200, 820)
        self.profile: spark.Profile | None = None
        self.heap: spark.Heap | None = None  # a heap summary instead of a profile
        self.name_ = ""
        self.thread: str | None = None
        self.windows: list[int] | None = None
        self.method: str | None = None  # None: the summary
        self.up = False  # callers instead of callees
        self.text = ""
        self.history: list[tuple] = []  # earlier views, for Esc: (method, up, windows, cursor row)

        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.source.lineEdit().setPlaceholderText(
            "spark.lucko.me link (profile or heap summary), its code, or a downloaded file; or pick a saved one"
        )
        for path in saved_profiles():
            self.source.addItem(path.name, str(path))
        self.source.setCurrentIndex(-1)
        self.source.lineEdit().returnPressed.connect(self.load)
        load = QPushButton("Load")
        load.clicked.connect(self.load)
        top = QHBoxLayout()
        top.addWidget(self.source, 1)
        top.addWidget(load)

        self.buttons: list[QPushButton] = []
        row = QHBoxLayout()
        for label, slot in (
            ("Summary (s)", self.summary),
            ("Calls (t)", lambda: self.tree(False)),
            ("Callers (c)", lambda: self.tree(True)),
            ("Back (Esc)", self.back),
            ("Copy for an LLM (y)", self.copy),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            button.setEnabled(False)
            row.addWidget(button)
            self.buttons.append(button)
        row.addStretch(1)

        self.status = QLabel("Paste a spark link or code and press Enter, or pick a saved profile.")
        self.report = Report(self._key)
        hint = QLabel(HINT)
        hint.setEnabled(False)  # greyed, like the TUI's dim hints

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addWidget(self.report, 1)
        layout.addWidget(hint)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.back)

    # --- running the spark module off the Qt thread

    def _call(self, fn: Callable[[], object], then: Callable, failed: Callable[[], None] | None = None) -> None:
        future = self.owner.loop.submit(asyncio.to_thread(fn))

        def done(f) -> None:
            if f.cancelled():
                return
            error = f.exception()
            if error is None:
                self.owner.post(lambda: then(f.result()))
                return

            def report() -> None:
                self._say(str(error), True)
                if failed:
                    failed()

            self.owner.post(report)

        future.add_done_callback(done)

    def _say(self, text: str, warning: bool = False) -> None:
        self.status.setText(text)
        self.status.setStyleSheet("color: #c62828;" if warning else "")

    # --- loading

    def _source(self) -> str:
        text = self.source.currentText().strip()
        index = self.source.findText(text)
        return self.source.itemData(index) if index >= 0 else text

    def load(self, refresh: bool = False) -> None:
        source = self._source()
        if not source:
            return
        self._say("Loading...")

        def work() -> tuple:
            upload, name = spark.load(source, refresh)
            if isinstance(upload, spark.Heap):
                return upload, name, spark.heap_report(upload, name)
            return upload, name, spark.summary(upload, name, None, None)

        self._call(work, self._loaded)

    def _loaded(self, result: tuple) -> None:
        upload, self.name_, text = result
        if isinstance(upload, spark.Heap):
            self.heap, self.profile = upload, None
        else:
            self.profile, self.heap = upload, None
        self.thread = self.windows = self.method = None
        self.up = False
        self.history = []
        for i, button in enumerate(self.buttons):  # a heap summary has no call trees
            button.setEnabled(self.heap is None or i in (0, 3, 4))
        self._put(text, 0)
        self.report.setFocus()

    # --- views

    def _view(self) -> Callable[[], str]:
        name, method, up, thread, windows = self.name_, self.method, self.up, self.thread, self.windows
        if self.heap is not None:
            heap = self.heap
            return lambda: spark.heap_report(heap, name)
        profile = self.profile
        if method is None:
            return lambda: spark.summary(profile, name, thread, windows)
        return lambda: spark.focus_report(profile, name, method, up, thread, windows)

    def _put(self, text: str, row: int) -> None:
        self.text = text
        self.report.show_text(text, row)
        where = [self.name_, self.thread or "server thread"]
        if self.windows:
            where.append(f"minute {', '.join(map(str, self.windows))}")
        if self.heap is not None:
            view = "heap summary"
        elif self.method is None:
            view = "summary"
        else:
            view = f"{'callers of' if self.up else 'calls of'} {self.method}"
        self._say(f"{' · '.join(where)}   ·   {view}")

    def _go(self, **changes: object) -> None:
        """Switch to another view, remembering this one for Esc."""
        before = (self.method, self.up, self.windows, self.report.row())
        for key, value in changes.items():
            setattr(self, key, value)

        def shown(text: str) -> None:
            self.history.append(before)
            self._put(text, 0)

        def failed() -> None:
            self.method, self.up, self.windows, _ = before

        self._call(self._view(), shown, failed)

    # --- actions

    def back(self) -> None:
        if not self.history:
            self.close()
            return
        self.method, self.up, self.windows, row = self.history.pop()
        self._call(self._view(), lambda text: self._put(text, row))

    def summary(self) -> None:
        """Straight back to the summary (minute kept); Esc then closes."""
        if self.profile is None and self.heap is None:
            return
        self.history.clear()
        self.method = None
        self._call(self._view(), lambda text: self._put(text, 0))

    def dig(self) -> None:
        if self.profile is None:
            return
        line = self.report.line()
        window = window_on_line(line)
        if window is not None:
            self._go(windows=None if self.windows == [window] else [window])
            return
        method = method_on_line(line)
        if method:
            self._go(method=method, up=False)

    def tree(self, up: bool) -> None:
        if self.profile is None:
            return
        method = method_on_line(self.report.line())
        if not method:
            self._say("Put the cursor on a line with a method first", True)
            return
        self._go(method=method, up=up)

    def copy(self) -> None:
        if self.text:
            QApplication.clipboard().setText(self.text)
            self._say(f"Copied {self.text.count(chr(10)) + 1} lines")

    def _key(self, key: int, text: str) -> bool:
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.dig()
            return True
        actions = {"t": lambda: self.tree(False), "c": lambda: self.tree(True), "s": self.summary, "y": self.copy}
        if text in actions:
            actions[text]()
            return True
        if key == Qt.Key.Key_Backspace:
            self.back()
            return True
        return False
