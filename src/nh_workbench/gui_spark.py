"""The spark tool of the desktop UI's Tools tab: a spark profile as an overview, browsable tables and call trees,
and the text report you copy for an LLM.

The text report is the counterpart of tui_spark.SparkScreen, on the same spark module and report line parsing.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QFontDatabase, QStandardItem, QStandardItemModel, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableView,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import keys as keytable
from . import spark
from .gui_spark_widgets import (
    BAD,
    BAR_ROLE,
    OK,
    SERIES,
    SORT_ROLE,
    WARN,
    BarDelegate,
    CallTreeModel,
    Card,
    Findings,
    Ink,
    PartsBar,
    ReportHighlighter,
    Timeline,
    expand_hot,
    ink,
)
from .spark import method_on_line, saved_profiles, when_text, window_on_line

if TYPE_CHECKING:
    from .gui import MainWindow

SAVED_COLUMNS = ["Profile", "Kind", "Recorded", "Downloaded", ""]
DELETE_COLUMN = 4  # the cross that deletes a saved file
# {calls}, {callers}, {summary} and {copy} are the keys set in Settings > Keys.
HINTS = {
    "overview": "Click a minute in the chart to show only that minute in every tab   {copy}: copy the report for an"
    " LLM",
    "calls": "{calls}: what the method calls   {callers}: who calls it   right click for more   Esc: whole thread",
    "methods": "Double click: what the method calls   right click: who calls it   type to filter",
    "mods": "Own time: time in the mod's own code, library calls it makes included. Double click: its methods",
    "entities": "Right click or double click a chunk to copy a /tp command to it",
    "heap": "Compare with a later (or earlier) heap summary to see what grows   Arrays count for their element type"
    " in the per-mod totals",
    "report": "Enter, double click or {calls} on a method: what it calls   {callers}: who calls it   Enter on a minute:"
    " only that minute   Esc or Backspace: back   {summary}: summary   {copy}: copy for an LLM",
}


def _plain(event) -> bool:
    """No modifier but Shift (letters keep their case: f and F are different keys)."""
    mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
    return mods in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier)


@dataclass
class Digest:
    """What the tabs show for one thread and set of minutes, worked out off the Qt thread."""

    view: spark.View
    findings: list[spark.Finding]
    parts: dict[str, tuple[float, dict[str, float]]]
    methods: list[tuple[str, str, float, float]]  # key, mod, own, total
    mods: list[tuple[str, float, str]]  # mod, own, its method with the most own time
    tree: spark.Merged


def digest(p: spark.Profile, thread: str | None, windows: list[int] | None) -> Digest:
    view = spark.View(p, spark.pick_thread(p, thread), windows)
    own, total = view.own_by_method(), view.total_by_method()
    owners: dict[str, str] = {}

    def mod(key: str) -> str:
        cls = key.rpartition(".")[0]
        if cls not in owners:
            owners[cls] = spark.package_of(cls, p.class_sources)
        return owners[cls]

    methods = [(k, mod(k), own.get(k, 0.0), t) for k, t in total.items() if t > 0]
    top: dict[str, tuple[str, float]] = {}
    for key, owner, mine, _ in methods:
        if mine > top.get(owner, ("", 0.0))[1]:
            top[owner] = (key, mine)
    by_mod = sorted(view.own_by_package().items(), key=lambda kv: -kv[1])
    mods = [(m, v, top.get(m, ("", 0))[0]) for m, v in by_mod if v > 0]
    tree = spark.callees(view, list(view.thread.roots))
    return Digest(view, spark.findings(p, view), spark.tick_parts(view), methods, mods, tree)


def _item(
    text: str, sort: object = None, bar: float | None = None, data: object = None, tip: str = ""
) -> QStandardItem:
    item = QStandardItem(text)
    item.setEditable(False)
    item.setData(text.lower() if sort is None else sort, SORT_ROLE)
    if bar is not None:
        item.setData(bar, BAR_ROLE)
    if data is not None:
        item.setData(data, Qt.ItemDataRole.UserRole)
    if tip:
        item.setToolTip(tip)
    if isinstance(sort, (int, float)):
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


class Table(QTableView):
    """A sortable, filterable table of QStandardItems, numbers sorted as numbers."""

    def __init__(self, columns: list[str], ink_of: Callable[[], Ink]) -> None:
        super().__init__()
        self.model_ = QStandardItemModel(0, len(columns))
        self.model_.setHorizontalHeaderLabels(columns)
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model_)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setModel(self.proxy)
        self.setItemDelegate(BarDelegate(self, ink_of))
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 8)
        self.horizontalHeader().setHighlightSections(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def fill(
        self, rows: list[list[QStandardItem]], sort_column: int, stretch: int = 0, wide: int = -1,
        columns: list[str] | None = None,
    ) -> None:  # fmt: skip
        """`stretch` takes the width left; `wide` starts wide but can be sized by hand (long method names).
        `columns` replaces the headers (and their number)."""
        self.setSortingEnabled(False)
        self.model_.removeRows(0, self.model_.rowCount())
        if columns is not None:
            self.model_.setColumnCount(len(columns))
            self.model_.setHorizontalHeaderLabels(columns)
        for row in rows:
            self.model_.appendRow(row)
        self.setSortingEnabled(True)
        self.sortByColumn(sort_column, Qt.SortOrder.DescendingOrder)
        header = self.horizontalHeader()
        for col in range(self.model_.columnCount()):
            mode = QHeaderView.ResizeMode.Stretch if col == stretch else QHeaderView.ResizeMode.ResizeToContents
            if col == wide:
                mode = QHeaderView.ResizeMode.Interactive
            header.setSectionResizeMode(col, mode)
        if wide >= 0 and not self.isColumnHidden(wide):
            self.setColumnWidth(wide, max(320, min(self.sizeHintForColumn(wide) + 16, self.width() // 2)))

    def cap(self, column: int, width: int) -> None:
        """Keep a column from growing past width (its text elided, the tooltip says it whole)."""
        if self.sizeHintForColumn(column) > width:
            self.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.setColumnWidth(column, width)

    def data_at(self, row: int | None = None) -> object:
        index = self.currentIndex() if row is None else self.proxy.index(row, 0)
        return self.proxy.index(index.row(), 0).data(Qt.ItemDataRole.UserRole) if index.isValid() else None


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
        plain = _plain(event)
        if plain and self.on_key(event.key(), event.text()):
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


class CallTree(QTreeView):
    """The call tree; t and c ask what the method on the current row calls, or who calls it."""

    def __init__(self, on_key: Callable[[str], bool]) -> None:
        super().__init__()
        self.on_key = on_key
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def keyPressEvent(self, event) -> None:
        text = event.text()
        plain = _plain(event)
        if plain and text and self.on_key(text):
            return
        super().keyPressEvent(event)


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("title")  # the bigger, bold style of the main window's titles
    return label


def _ms(value: float) -> str:
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms" if value >= 10 else f"{value:.1f} ms"


class SparkWindow(QWidget):
    """Load a profile, read its overview, browse where the time goes, and copy the text report for an LLM."""

    def __init__(self, owner: "MainWindow") -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.owner = owner  # its asyncio loop runs the downloads and reports
        self.setWindowTitle("spark profiles - NH Workbench")
        self.setWindowIcon(owner.windowIcon())
        self.resize(1280, 900)
        self.profile: spark.Profile | None = None
        self.heap: spark.Heap | None = None  # a heap summary instead of a profile
        self.other: tuple[spark.Heap, str] | None = None  # another heap summary it is compared with
        self.digest: Digest | None = None
        self.name_ = ""
        self.thread: str | None = None
        self.windows: list[int] | None = None
        self.method: str | None = None  # None: the summary
        self.up = False  # callers instead of callees
        self.text = ""
        self.history: list[tuple] = []  # earlier report views, for Esc: (method, up, windows, cursor row)
        self.focus: tuple[str, bool] | None = None  # the call tree tab: (method, callers) or the whole thread

        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.source.lineEdit().setPlaceholderText(
            "spark.lucko.me link (profile or heap summary), its code, or a downloaded file; or pick a saved one"
        )
        # The saved ones as a table in the popup; the box itself shows only the code (column 0).
        self.saved = QStandardItemModel(0, len(SAVED_COLUMNS))
        self.saved.setHorizontalHeaderLabels(SAVED_COLUMNS)
        self.source.setModel(self.saved)
        popup = QTableView()
        popup.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        popup.setShowGrid(False)
        popup.setWordWrap(False)
        popup.verticalHeader().hide()
        header = popup.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)  # over the cells
        self.source.setView(popup)
        # After the combo box's own filters, so these run first: a click on the cross must not pick the row.
        self.saved_view, self.saved_viewport = popup, popup.viewport()  # kept: the filter must not ask the combo box
        self.saved_viewport.installEventFilter(self)
        popup.installEventFilter(self)
        self._fill_saved()
        self.source.lineEdit().returnPressed.connect(self.load)
        load = QPushButton("Load")
        load.clicked.connect(self.load)
        top = QHBoxLayout()
        top.addWidget(self.source, 1)
        top.addWidget(load)

        self.threads = QComboBox()
        self.threads.setToolTip("The thread the tabs and the report show")
        # Filled after the window is first shown: the default would keep the width it had while empty.
        self.threads.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.threads.currentIndexChanged.connect(self._thread_picked)
        self.minute = QLabel("All minutes")
        self.all_minutes = QPushButton("Back to all minutes")
        self.all_minutes.clicked.connect(lambda: self.set_scope(windows=None))
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self.copy)
        scope = QHBoxLayout()
        self.thread_label = QLabel("Thread")
        scope.addWidget(self.thread_label)
        scope.addWidget(self.threads)
        scope.addSpacing(16)
        scope.addWidget(self.minute)
        scope.addWidget(self.all_minutes)
        scope.addStretch(1)
        scope.addWidget(self.copy_button)

        self.status = QLabel("Paste a spark link or code and press Enter, or pick a saved profile.")
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tab_keys: list[str] = []
        self._build_overview()
        self._build_calls()
        self._build_methods()
        self._build_mods()
        self._build_entities()
        self._build_heap()
        self._build_report()
        self.tabs.currentChanged.connect(self._tab_changed)
        self.hint = QLabel()
        self.hint.setEnabled(False)  # greyed, like the TUI's dim hints

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(scope)
        layout.addWidget(self.status)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.hint)
        self._enable()
        self._show_keys()

    def ink(self) -> Ink:
        return ink(self, self.owner.theme_name)

    # --- building the tabs

    def _add_tab(self, key: str, widget: QWidget, title: str) -> None:
        self.tab_keys.append(key)
        self.tabs.addTab(widget, title)

    def _build_overview(self) -> None:
        self.subtitle = QLabel()
        self.subtitle.setWordWrap(True)
        self.subtitle.setEnabled(False)
        self.cards = {key: Card(title, self.ink) for key, title in (
            ("tps", "TPS"), ("tick", "Tick time"), ("cpu", "CPU"),
            ("heap", "Memory"), ("gc", "GC pauses"), ("world", "World"),
        )}  # fmt: skip
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, i // 3, i % 3)
        self.findings = Findings(self.ink)
        self.parts_title = _heading("Where the server thread goes")
        self.parts = PartsBar(self.ink)
        self.timeline = Timeline(self.ink)
        self.timeline.picked.connect(lambda wid: self.set_scope(windows=None if wid is None else [wid]))
        self.machine = QLabel()
        self.machine.setWordWrap(True)
        self.machine.setTextFormat(Qt.TextFormat.RichText)
        self.machine.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(12, 10, 12, 12)
        column.setSpacing(8)
        column.addWidget(self.subtitle)
        column.addLayout(grid)
        column.addWidget(_heading("Findings"))
        column.addWidget(self.findings)
        column.addWidget(self.parts_title)
        column.addWidget(self.parts)
        column.addWidget(_heading("Each minute"))
        column.addWidget(self.timeline)
        column.addWidget(_heading("Machine and Java"))
        column.addWidget(self.machine)
        column.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self._add_tab("overview", scroll, "Overview")

    def _build_calls(self) -> None:
        self.find = QLineEdit()
        self.find.setPlaceholderText("A method, or part of its name; Enter: what it calls")
        self.find.returnPressed.connect(lambda: self._focus_find(False))
        calls = QPushButton("What it calls")
        calls.clicked.connect(lambda: self._focus_find(False))
        callers = QPushButton("Who calls it")
        callers.clicked.connect(lambda: self._focus_find(True))
        whole = QPushButton("Whole thread")
        whole.clicked.connect(lambda: self.show_tree(None))
        self.tree_title = QLabel()
        self.tree = CallTree(self._tree_key)
        self.tree.setItemDelegate(BarDelegate(self.tree, self.ink))
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        row = QHBoxLayout()
        row.addWidget(self.find, 1)
        for button in (calls, callers, whole):
            row.addWidget(button)
        page = QWidget()
        column = QVBoxLayout(page)
        column.addLayout(row)
        column.addWidget(self.tree_title)
        column.addWidget(self.tree, 1)
        self._add_tab("calls", page, "Call tree")

    def _build_methods(self) -> None:
        self.methods_filter = QLineEdit()
        self.methods_filter.setPlaceholderText("Filter by method, class or mod")
        self.methods = Table(["Method", "Own", "Total", "Mod"], self.ink)
        self.methods_filter.textChanged.connect(self.methods.proxy.setFilterFixedString)
        self.methods.doubleClicked.connect(lambda index: self._method_action(self.methods.data_at(), False))
        self.methods.customContextMenuRequested.connect(lambda pos: self._method_menu(self.methods, pos))
        page = QWidget()
        column = QVBoxLayout(page)
        column.addWidget(self.methods_filter)
        column.addWidget(self.methods, 1)
        self._add_tab("methods", page, "Methods")

    def _build_mods(self) -> None:
        self.mods = Table(["Mod", "Own time", "Its busiest method"], self.ink)
        self.mods.doubleClicked.connect(lambda index: self._mod_methods(self.mods.data_at()))
        self._add_tab("mods", self.mods, "Mods")

    def _build_entities(self) -> None:
        self.entity_types = Table(["Entity", "Count"], self.ink)
        self.entity_worlds = Table(["World", "Entities"], self.ink)
        self.chunks = Table(["Entities", "World", "x", "z", "Most"], self.ink)
        self.chunks.doubleClicked.connect(lambda index: self._copy_tp(self.chunks.data_at()))
        self.chunks.customContextMenuRequested.connect(self._chunk_menu)
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.entity_types)
        left.addWidget(self.entity_worlds)
        left.setSizes([600, 200])
        right = QWidget()
        column = QVBoxLayout(right)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(QLabel("Busiest chunks (block coordinates of the chunk centre)"))
        column.addWidget(self.chunks, 1)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([400, 800])
        self._add_tab("entities", split, "Entities")

    def _build_heap(self) -> None:
        self.heap_classes = Table(["Class", "Mod", "Objects", "Size"], self.ink)
        self.heap_mods = Table(["Mod", "Objects", "Size"], self.ink)
        self.compare_source = QComboBox()
        self.compare_source.setEditable(True)
        self.compare_source.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.compare_source.lineEdit().setPlaceholderText("Another heap summary: link, code, file, or a saved one")
        self.compare_source.lineEdit().returnPressed.connect(self.compare)
        compare = QPushButton("Compare")
        compare.setToolTip("What grew between the two: the earlier one is taken as the start, whichever you load")
        compare.clicked.connect(self.compare)
        self.stop_compare = QPushButton("Stop comparing")
        self.stop_compare.clicked.connect(lambda: self._compared(None))
        self.heap_summary = QLabel()
        row = QHBoxLayout()
        row.addWidget(QLabel("Compare with"))
        row.addWidget(self.compare_source, 1)
        row.addWidget(compare)
        row.addWidget(self.stop_compare)
        split = QSplitter()
        split.addWidget(self.heap_classes)
        split.addWidget(self.heap_mods)
        split.setSizes([800, 400])
        page = QWidget()
        column = QVBoxLayout(page)
        column.addLayout(row)
        column.addWidget(self.heap_summary)
        column.addWidget(split, 1)
        self._add_tab("heap", page, "Memory by class")

    def _build_report(self) -> None:
        # What it calls / who calls it are Enter, t and c on a line: only the way back needs buttons.
        self.back_button = QPushButton("Back (Esc)")
        self.back_button.clicked.connect(self.back)
        self.summary_button = QPushButton()
        self.summary_button.clicked.connect(self.summary)
        row = QHBoxLayout()
        row.addWidget(self.back_button)
        row.addWidget(self.summary_button)
        row.addStretch(1)
        self.report = Report(self._key)
        self.highlighter = ReportHighlighter(self.report.document(), self.ink)
        page = QWidget()
        column = QVBoxLayout(page)
        column.addLayout(row)
        column.addWidget(self.report, 1)
        self._add_tab("report", page, "Text report")

    def show_tab(self, key: str) -> None:
        self.tabs.setCurrentIndex(self.tab_keys.index(key))

    def _tab_changed(self, index: int) -> None:
        if 0 <= index < len(self.tab_keys):
            self._show_keys()
            if self.tab_keys[index] == "report":
                self.report.setFocus()  # its keys work straight away, not in the link box

    def _enable(self) -> None:
        """Only the tabs and buttons that make sense for what is loaded."""
        profile, heap = self.profile is not None, self.heap is not None
        shown = {
            "overview": profile, "calls": profile, "methods": profile, "mods": profile,
            "entities": profile and bool(self.profile.entities), "heap": heap, "report": profile or heap,
        }  # fmt: skip
        for i, key in enumerate(self.tab_keys):
            self.tabs.setTabVisible(i, shown[key])
        self._enable_report_buttons()
        self.threads.setVisible(profile)  # a heap summary has no threads
        self.thread_label.setVisible(profile)
        self.copy_button.setEnabled(profile or heap)
        self.all_minutes.setVisible(bool(self.windows))
        self.minute.setVisible(profile)

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
        self.status.setStyleSheet(f"color: {self.owner.warning_color()};" if warning else "")

    # --- loading

    def _fill_saved(self) -> None:
        """List the saved profiles again: downloads since the window was made are new ones."""
        text = self.source.currentText()
        self.source.blockSignals(True)
        self.saved.removeRows(0, self.saved.rowCount())
        for path in saved_profiles():  # last downloaded first
            info = spark.saved_info(path)
            recorded = when_text(info.recorded / 1000) if info.recorded else "?"
            if info.length_ms:
                recorded += f"  for {spark.span_text(info.length_ms)}"
            code = QStandardItem(path.stem)
            code.setData(str(path), Qt.ItemDataRole.UserRole)
            code.setToolTip(str(path))
            cross = QStandardItem("✕")
            cross.setToolTip("Delete this saved file (asks first)")
            cross.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            row = [code, QStandardItem(info.kind), QStandardItem(recorded), QStandardItem(when_text(info.downloaded))]
            self.saved.appendRow([*row, cross])
        self.source.setCurrentIndex(-1)
        self.source.setEditText(text)
        self.source.blockSignals(False)
        popup = self.source.view()
        header = popup.horizontalHeader()
        resize = QHeaderView.ResizeMode
        for col in range(len(SAVED_COLUMNS)):  # the last text column takes the room: the cross stays at the right
            header.setSectionResizeMode(col, resize.Stretch if col == DELETE_COLUMN - 1 else resize.ResizeToContents)
        width = sum(popup.sizeHintForColumn(c) for c in range(len(SAVED_COLUMNS)))
        popup.setMinimumWidth(min(width + popup.verticalScrollBar().sizeHint().width() + 40, 1000))

    def eventFilter(self, obj, event) -> bool:
        """The saved list's cross (or Delete on a row) deletes that file, after asking.

        Only Python references are compared before acting: while the window is torn down, the combo box is gone.
        """
        popup = self.saved_view
        kind = event.type()
        if obj is self.saved_viewport and kind in (
            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick
        ):  # fmt: skip
            index = popup.indexAt(event.position().toPoint())
            if index.isValid() and index.column() == DELETE_COLUMN:
                if kind == QEvent.Type.MouseButtonRelease:
                    row = index.row()
                    QTimer.singleShot(0, lambda: self._delete_saved(row))
                return True
        if obj is popup and kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Delete:
            row = popup.currentIndex().row()
            if row >= 0:
                QTimer.singleShot(0, lambda: self._delete_saved(row))
                return True
        return super().eventFilter(obj, event)

    def _confirm_delete(self, name: str, kind: str) -> bool:
        question = f"Delete the saved {kind} {name}?"
        box = QMessageBox(QMessageBox.Icon.Question, "Delete saved file", question, parent=self)
        box.setInformativeText(
            "It is removed from the cache folder. Its link downloads it again for as long as spark.lucko.me keeps it."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _delete_saved(self, row: int) -> None:
        self.source.hidePopup()
        code = self.saved.item(row, 0)
        if code is None:
            return
        name, path = code.text(), Path(code.data(Qt.ItemDataRole.UserRole))
        if not self._confirm_delete(name, self.saved.item(row, 1).text()):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            self._say(f"Could not delete {path}: {e}", True)
            return
        if self.source.currentText().strip() == name:
            self.source.setEditText("")
        self._fill_saved()
        self._say(f"Deleted the saved file {name}")
        if self.saved.rowCount():
            self.source.showPopup()  # to delete another

    def showEvent(self, event) -> None:
        self._fill_saved()
        self._fill_compare()
        self._show_keys()
        super().showEvent(event)

    def letters(self) -> dict[str, str]:
        """The keys set in Settings > Keys, read each time: a change there applies at once."""
        return keytable.letters(self.owner.cfg)

    def _show_keys(self) -> None:
        """The hint and the buttons that name a key, with the current keys."""
        k = self.letters()
        named = {name: k[f"spark_{name}"] for name in ("calls", "callers", "summary", "copy")}
        index = self.tabs.currentIndex()
        tab = self.tab_keys[index] if 0 <= index < len(self.tab_keys) else "overview"
        self.hint.setText(HINTS[tab].format(**named))
        self.copy_button.setText(f"Copy report for an LLM ({named['copy']})")
        self.summary_button.setText(f"Summary ({named['summary']})")

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
                return upload, name, spark.heap_report(upload, name), None
            return upload, name, spark.summary(upload, name, None, None), digest(upload, None, None)

        self._call(work, self._loaded)

    def _loaded(self, result: tuple) -> None:
        upload, self.name_, text, found = result
        self._fill_saved()
        if isinstance(upload, spark.Heap):
            self.heap, self.profile = upload, None
        else:
            self.profile, self.heap = upload, None
        self.thread = self.windows = self.method = None
        self.up = False
        self.history = []
        self.focus = None
        self.other = None
        self._fill_compare()
        self._fill_threads()
        self._enable()
        self._put(text, 0)
        if self.heap is not None:
            self._show_heap()
            self.show_tab("heap")
        else:
            self._show_digest(found)
            self.show_tab("overview")

    def _fill_threads(self) -> None:
        self.threads.blockSignals(True)
        self.threads.clear()
        if self.profile is not None and self.profile.threads:
            first = spark.pick_thread(self.profile, None)
            weights = {t.name: sum(t.times) for t in self.profile.threads}
            whole = sum(weights.values()) or 1
            for t in [first] + [t for t in spark.busiest_threads(self.profile) if t is not first]:
                share = f"  ({100 * weights[t.name] / whole:.0f}%)" if len(self.profile.threads) > 1 else ""
                self.threads.addItem(t.name + share, t.name)
        self.threads.blockSignals(False)

    # --- the thread and minutes every tab shows

    def _thread_picked(self, index: int) -> None:
        if index >= 0:
            self.set_scope(thread=self.threads.itemData(index))

    def set_scope(self, **changes: object) -> None:
        """Another thread or other minutes: every tab again, the report back to its summary."""
        if self.profile is None:
            return
        for key, value in changes.items():
            setattr(self, key, value)
        self.method, self.up, self.history = None, False, []
        profile, name, thread, windows = self.profile, self.name_, self.thread, self.windows

        def work() -> tuple:
            return spark.summary(profile, name, thread, windows), digest(profile, thread, windows)

        def shown(result: tuple) -> None:
            text, found = result
            self._put(text, 0)
            self._show_digest(found)

        self._say("Working...")
        self._call(work, shown)

    def _refresh_digest(self) -> None:
        profile, thread, windows = self.profile, self.thread, self.windows
        if profile is not None:
            self._call(lambda: digest(profile, thread, windows), self._show_digest)

    # --- filling the tabs

    def _show_digest(self, found: Digest) -> None:
        p = self.profile
        if p is None or found is None:
            return
        self.digest = found
        self._enable()
        self.minute.setText(self._minute_text())
        self._show_overview(p, found)
        self.show_tree(self.focus)
        self._show_methods(found)
        self._show_mods(found)
        self._show_entities(p)

    def _minute_text(self) -> str:
        if not self.windows:
            return "All minutes"
        p = self.profile
        by_id = {w.id: w for w in p.windows} if p else {}
        at = [
            spark.span_text(by_id[w].start - p.start) for w in self.windows if w in by_id and by_id[w].start and p.start
        ]
        return f"Only minute {', '.join(map(str, self.windows))}" + (f" (at {', '.join(at)})" if at else "")

    def _picked_windows(self) -> list[spark.Window]:
        p = self.profile
        if p is None:
            return []
        if self.windows:
            return [w for w in p.windows if w.id in self.windows and w.ticks]
        return []

    def _show_overview(self, p: spark.Profile, found: Digest) -> None:
        when = datetime.fromtimestamp(p.start / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC") if p.start else "?"
        mode = (
            f"only ticks over {p.tick_threshold_ms:g} ms were kept ({p.included_ticks} of {p.ticks}):"
            " shares describe those slow ticks, not normal play"
            if p.ticked
            else f"every tick kept ({p.ticks})"
            if p.ticks
            else ""
        )
        mc = f"Minecraft {p.minecraft}" if p.minecraft else ""
        recorded = f"recorded {when} for {spark.span_text(p.duration_ms)}"
        parts = [self.name_, f"{p.platform} {p.platform_version}".strip(), mc, recorded, mode]
        parts += spark.key_mods(p)
        if p.comment:
            parts.append(f"comment: {p.comment}")
        self.subtitle.setText("   ·   ".join(x for x in parts if x))
        self._fill_cards(p)
        self.findings.show_findings(found.findings)
        self._fill_parts(p, found)
        self.timeline.set_profile(p, self.windows)
        self.machine.setText(self._machine_html(p))

    def _fill_cards(self, p: spark.Profile) -> None:
        picked = self._picked_windows()
        which = "the picked minute" if picked else "the last minute"
        c = self.cards
        if picked:
            tps = sum(w.tps for w in picked) / len(picked)
            c["tps"].set(f"{tps:.1f}", f"in {which}", *self._tps_state(tps))
        elif any(p.tps):
            c["tps"].set(
                f"{p.tps[0]:.1f}", f"5 min {p.tps[1]:.1f}   ·   15 min {p.tps[2]:.1f}", *self._tps_state(p.tps[0])
            )
        else:
            c["tps"].set("-", "not reported")
        if picked:
            median = sum(w.mspt_median for w in picked) / len(picked)
            slowest = max(w.mspt_max for w in picked)
            state, label = (BAD, "over budget") if median >= 50 else (WARN, "froze") if slowest >= 1000 else (OK, "")
            c["tick"].set(_ms(median), f"median; slowest {_ms(slowest)} in {which}", state, label)
        elif p.mspt.get("median"):
            m = p.mspt
            if m["median"] >= 50:
                state, label = BAD, "over budget"
            elif m["max"] >= 1000:
                state, label = BAD, "froze"
            elif m["p95"] >= 50:
                state, label = WARN, "stutters"
            else:
                state, label = OK, "smooth"
            tip = "A tick may take 50 ms for 20 TPS"
            detail = f"median; p95 {_ms(m['p95'])}, slowest {_ms(m['max'])} in {which}"
            c["tick"].set(_ms(m["median"]), detail, state, label, tip)
        else:
            c["tick"].set("-", "not reported")
        process = sum(w.cpu_process for w in picked) / len(picked) if picked else p.cpu_process[0]
        if not spark.cpu_reported(p):
            c["cpu"].set("-", f"use not reported; {p.cpu_threads} threads" if p.cpu_threads else "not reported")
        elif p.cpu_threads or process:
            busy = p.cpu_system[0] >= 0.9 and p.cpu_process[0] < 0.6 * p.cpu_system[0]
            few = 0 < p.cpu_threads <= 2
            state, label = (WARN, "few threads") if few else (WARN, "busy machine") if busy else ("", "")
            detail = f"of the machine, by the game; machine {100 * p.cpu_system[0]:.0f}%, {p.cpu_threads} threads"
            c["cpu"].set(f"{100 * process:.0f}%", detail, state, label, p.cpu_model)
        else:
            c["cpu"].set("-", "not reported")
        if p.heap_total:
            limit = p.heap_max or p.heap_total
            state, label = "", ""
            if p.old_gen_live and p.old_gen_live >= 0.8 * limit:
                state, label = BAD, "nearly full"
            elif p.heap_max and p.heap_max < 4e9:
                state, label = WARN, "small"
            detail = f"used of {spark.bytes_text(limit)} max; {spark.bytes_text(p.heap_total)} allocated"
            if p.old_gen_live:
                detail += f", {spark.bytes_text(p.old_gen_live)} after the last GC"
            c["heap"].set(spark.bytes_text(p.heap_used), detail, state, label)
        else:
            c["heap"].set("-", "not reported")
        during = any(g.collections for g in p.gc.values())
        gcs = {n: g for n, g in (p.gc if during else p.gc_since_start).items() if g.collections}
        if gcs:
            pausing = {n: g for n, g in gcs.items() if spark.pauses_game(n)} or gcs
            worst = max(pausing.values(), key=lambda g: g.avg_ms)
            state = BAD if worst.avg_ms >= 1000 else WARN if worst.avg_ms >= 200 else OK
            label = {BAD: "freezes", WARN: "long", OK: "short"}[state]
            if state != OK and spark.lone_gc(worst, during):
                state, label = "", "once, likely at world load"
            listed = ", ".join(f"{n} {g.collections}x {_ms(g.avg_ms)}" for n, g in gcs.items())
            detail = f"longest average; {'during the profile' if during else 'since start'}: {listed}"
            c["gc"].set(_ms(worst.avg_ms), detail, state, label)
        else:
            c["gc"].set("-", "no collections reported")
        last = picked[-1] if picked else next((w for w in reversed(p.windows) if w.ticks), None)
        players = last.players if last else p.players
        entities = last.entities if last else p.entities
        chunks = last.chunks if last else -1
        if players >= 0:
            loaded = f", {chunks} chunks loaded" if chunks >= 0 else ""
            c["world"].set(f"{players} player{'s' if players != 1 else ''}", f"{entities} entities{loaded}")
        else:  # a client reports no players or entities of its own
            c["world"].set(f"{chunks} chunks" if chunks >= 0 else "-", "loaded" if chunks >= 0 else "not reported")

    def _tps_state(self, tps: float) -> tuple[str, str]:
        if self.profile is not None and self.profile.client:  # a client's own ticks: no server falls behind
            return "", ""
        if tps >= 19.5:
            return OK, "full speed"
        return (WARN, "behind") if tps >= 15 else (BAD, "far behind")

    def _fill_parts(self, p: spark.Profile, found: Digest) -> None:
        view, whole = found.view, found.view.whole
        thread = view.thread.name.lower()
        self.parts_title.setText(f"Where the {thread} goes" + (" (slow ticks only)" if p.ticked else ""))
        shown, idle, accounted = [], [], 0.0
        for label, _ in spark.TICK_PARTS:  # colours in this fixed order; past the last colour, "other work"
            total, by_mod = found.parts.get(label, (0.0, {}))
            if total < whole * 0.005 or (label != spark.IDLE and len(shown) == len(SERIES)):
                continue
            accounted += total
            mods = spark.top_mods_text(by_mod, whole, 5)
            tip = f"\n{mods}" if mods else ""
            if label == spark.IDLE:
                idle.append((label, total / whole, tip, -2))
            else:
                shown.append((label, total / whole, tip, len(shown)))
        other = max(0.0, whole - accounted)
        if other >= whole * 0.005:
            shown.append(("other work", other / whole, "\nTicking worlds, players, events and the rest", -1))
        self.parts.set_parts(shown + idle)
        self.parts.setVisible(not p.allocation)
        self.parts_title.setVisible(not p.allocation)

    def _machine_html(self, p: spark.Profile) -> str:
        rows = []
        if p.cpu_model or p.cpu_threads:
            rows.append(("CPU", f"{p.cpu_model or '?'}, {p.cpu_threads} threads"))
        if p.ram_total:
            rows.append(("RAM", f"{spark.bytes_text(p.ram_used)} of {spark.bytes_text(p.ram_total)} in use"))
        if p.swap_total:
            rows.append(("Swap", f"{spark.bytes_text(p.swap_used)} of {spark.bytes_text(p.swap_total)} in use"))
        if p.disk_total:
            rows.append(("Disk", f"{spark.bytes_text(p.disk_used)} of {spark.bytes_text(p.disk_total)} in use"))
        if p.os:
            rows.append(("OS", p.os))
        if p.java:
            rows.append(("Java", p.java + (f" ({p.jvm})" if p.jvm else "")))
        if p.non_heap:
            rows.append(("Non-heap", spark.bytes_text(p.non_heap)))
        if p.uptime_ms:
            rows.append(("Up for", spark.span_text(p.uptime_ms)))
        engine = {"async": "async-profiler", "java": "spark's Java sampler"}.get(p.engine, "")
        what = "allocations" if p.allocation else f"every {p.interval_us / 1000:g} ms"
        rows.append(("Sampling", f"{what}" + (f" with {engine}" if engine else "")))
        if p.vm_args:
            args = [a for a in p.vm_args.split() if not a.startswith(("--add-opens", "--add-exports"))]
            rows.append(("JVM args", " ".join(args)))
        cells = "".join(
            f"<tr><td style='padding:1px 16px 1px 0; white-space:nowrap'><b>{escape(k)}</b></td>"
            f"<td style='padding:1px 0'>{escape(v)}</td></tr>"
            for k, v in rows
        )
        return f"<table>{cells}</table>"

    def _show_methods(self, found: Digest) -> None:
        whole = found.view.whole
        rows = []
        for key, mod, own, total in found.methods:
            rows.append([
                _item(spark.short_name(key), data=key, tip=key),
                _item(f"{100 * own / whole:.1f}%", own, own / whole),
                _item(f"{100 * total / whole:.1f}%", total, total / whole),
                _item(mod.replace(" (package)", "?")),
            ])  # fmt: skip
        self.methods.fill(rows, 1, stretch=3, wide=0)

    def _show_mods(self, found: Digest) -> None:
        whole = found.view.whole
        rows = [
            [
                _item(mod.replace(" (package)", "?"), data=mod),
                _item(f"{100 * own / whole:.1f}%", own, own / whole),
                _item(spark.short_name(key) if key else "", tip=key),
            ]
            for mod, own, key in found.mods
        ]
        self.mods.fill(rows, 1, stretch=2)

    def _show_entities(self, p: spark.Profile) -> None:
        top = max(p.entity_counts.values(), default=1)
        self.entity_types.fill([[_item(k), _item(str(v), v, v / top)] for k, v in p.entity_counts.items()], 1)
        top = max(p.world_entities.values(), default=1) or 1
        worlds = [[_item(k), _item(str(v), v, v / top)] for k, v in p.world_entities.items() if v]
        self.entity_worlds.fill(worlds, 1)
        top = max((c.total for c in p.chunks), default=1) or 1
        rows = []
        for c in p.chunks:
            if not c.total:
                continue
            x, z = c.x * 16 + 8, c.z * 16 + 8
            rows.append([
                _item(str(c.total), c.total, c.total / top, data=(x, z)),
                _item(c.world), _item(str(x), x), _item(str(z), z), _item(spark.counts_text(c.counts, 4)),
            ])  # fmt: skip
        self.chunks.fill(rows, 0, stretch=4)

    def _pair(self) -> tuple[tuple[spark.Heap, str], tuple[spark.Heap, str]] | None:
        """The two summaries compared, earlier first; None when not comparing."""
        if self.heap is None or self.other is None:
            return None
        return spark.heap_in_order((self.heap, self.name_), self.other)

    def _show_heap(self) -> None:
        h = self.heap
        if h is None:
            return
        self.stop_compare.setVisible(self.other is not None)
        pair = self._pair()
        if pair is not None:
            self._show_growth(*pair)
            return
        self.heap_summary.setText(f"{spark.bytes_text(h.size)} in {len(h.entries):,} classes")
        self.heap_summary.setStyleSheet("")
        total = h.size or 1
        top = max((e.size for e in h.entries), default=1) or 1
        rows = [
            [
                _item(e.cls, data=e.cls),
                _item(owner := spark.heap_owner(e.cls).replace(" (package)", "?"), tip=owner),
                _item(f"{e.instances:,}", e.instances),
                _item(spark.bytes_text(e.size), e.size, e.size / top, tip=f"{100 * e.size / total:.1f}% of the heap"),
            ]
            for e in h.entries
        ]
        self.heap_classes.fill(rows, 3, columns=["Class", "Mod", "Objects", "Size"])
        self.heap_classes.cap(1, 180)  # long mod names would squeeze the class names
        owners = spark.heap_by_owner(h.entries)
        top = max((s for s, _ in owners.values()), default=1) or 1
        rows = [
            [_item(o.replace(" (package)", "?")), _item(f"{n:,}", n), _item(spark.bytes_text(s), s, s / top)]
            for o, (s, n) in owners.items()
        ]
        self.heap_mods.fill(rows, 2, columns=["Mod", "Objects", "Size"])

    def _show_growth(self, a: tuple[spark.Heap, str], b: tuple[spark.Heap, str]) -> None:
        """Both summaries side by side: what each class and mod holds now, and how much it grew."""
        (ha, na), (hb, nb) = a, b
        change = hb.size - ha.size
        apart = f" over {spark.span_text(hb.generated - ha.generated)}" if ha.generated and hb.generated else ""
        summary = (
            f"{na} → {nb}: {spark.bytes_text(ha.size)} → {spark.bytes_text(hb.size)}"
            f" ({'+' if change >= 0 else '-'}{spark.bytes_text(abs(change))}){apart}. "
        )
        if spark.heap_restarted(ha, hb):
            self.heap_summary.setText(summary + spark.RESTARTED.format(up=spark.span_text(hb.uptime_ms)))
            self.heap_summary.setStyleSheet(f"color: {self.owner.warning_color()};")
        else:
            self.heap_summary.setText(summary + "What keeps growing is what leaks; one change alone can be noise.")
            self.heap_summary.setStyleSheet("")
        self.heap_summary.setWordWrap(True)

        def signed(n: int, text: str) -> str:
            return ("+" if n > 0 else "-" if n < 0 else "") + text

        deltas = spark.heap_deltas(ha, hb)
        top = max((size for _, size, _, _ in deltas), default=1) or 1
        rows = []
        for cls, size, count, now in deltas:
            rows.append([
                _item(cls, data=cls),
                _item(owner := spark.heap_owner(cls).replace(" (package)", "?"), tip=owner),
                _item(f"{now.instances:,}" if now else "gone", now.instances if now else 0),
                _item(signed(count, f"{abs(count):,}"), count),
                _item(spark.bytes_text(now.size) if now else "gone", now.size if now else 0),
                _item(signed(size, spark.bytes_text(abs(size))), size, max(0, size) / top),
            ])  # fmt: skip
        self.heap_classes.fill(rows, 5, columns=["Class", "Mod", "Objects", "Objects +", "Size", "Size +"])
        self.heap_classes.cap(1, 180)  # long mod names would squeeze the class names
        owners = spark.heap_by_owner(hb.entries)
        growth = spark.heap_owner_deltas(ha, hb)
        top = max((size for size, _ in growth.values()), default=1) or 1
        rows = []
        for owner, (size, count) in growth.items():
            now = owners.get(owner, [0, 0])
            rows.append([
                _item(owner.replace(" (package)", "?")),
                _item(spark.bytes_text(now[0]), now[0]),
                _item(signed(count, f"{abs(count):,}"), count),
                _item(signed(size, spark.bytes_text(abs(size))), size, max(0, size) / top),
            ])  # fmt: skip
        self.heap_mods.fill(rows, 3, columns=["Mod", "Size", "Objects +", "Size +"])

    def _fill_compare(self) -> None:
        """The other saved heap summaries, to compare with."""
        text = self.compare_source.currentText()
        self.compare_source.blockSignals(True)
        self.compare_source.clear()
        for path in saved_profiles():
            if path.suffix != ".sparkheap" or path.stem == self.name_:
                continue
            info = spark.saved_info(path)
            when = f"  recorded {when_text(info.recorded / 1000)}" if info.recorded else ""
            self.compare_source.addItem(f"{path.stem}{when}", str(path))
        self.compare_source.setCurrentIndex(-1)
        self.compare_source.setEditText(text)
        self.compare_source.blockSignals(False)

    def compare(self) -> None:
        text = self.compare_source.currentText().strip()
        if self.heap is None or not text:
            return
        index = self.compare_source.findText(text)
        source = self.compare_source.itemData(index) if index >= 0 else text
        self._say("Loading the heap summary to compare with...")
        self._call(lambda: spark.load_heap(source), self._compared)

    def _compared(self, other: tuple[spark.Heap, str] | None) -> None:
        """Start comparing with other, or stop (None); the report follows."""
        if other is not None and other[1] == self.name_:
            self._say("That is the heap summary already shown: pick another one", True)
            return
        self.other = other
        self.history.clear()
        self.method = None
        self._show_heap()
        self._call(self._view(), lambda text: self._put(text, 0))

    # --- the call tree tab

    def show_tree(self, focus: tuple[str, bool] | None) -> None:
        found = self.digest
        if found is None:
            return
        view = found.view
        if focus is None:
            top, up = found.tree, False
            self.tree_title.setText(f"Everything the {view.thread.name.lower()} does")
        else:
            method, up = focus
            starts = view.matches(method)
            if not starts:
                self._say(f"No method matching {method!r} in {view.thread.name}", True)
                return
            top = (spark.callers if up else spark.callees)(view, starts)
            keys = sorted({view.thread.frames[i].key for i in starts})
            what = "Who calls" if up else "What is called by"
            more = f" and {len(keys) - 1} more" if len(keys) > 1 else ""
            share = f"{100 * top.total / view.whole:.1f}% of the thread"
            self.tree_title.setText(f"{what} {spark.short_name(keys[0])}{more}: {share}")
        self.focus = focus
        model = CallTreeModel(top, view, up)
        self.tree.setModel(model)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(model.COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        expand_hot(self.tree, model, 0.05 if focus is None else 0.01)

    def _focus_find(self, up: bool) -> None:
        text = self.find.text().strip()
        if text:
            self.show_tree((text, up))
            self.tree.setFocus()

    def _tree_method(self) -> str | None:
        model = self.tree.model()
        index = self.tree.currentIndex()
        return model.key(index) if isinstance(model, CallTreeModel) and index.isValid() else None

    def _tree_key(self, text: str) -> bool:
        k = self.letters()
        method = self._tree_method()
        if text in (k["spark_calls"], k["spark_callers"]) and method:
            self._method_action(method, text == k["spark_callers"])
            return True
        if text == k["spark_copy"]:
            self.copy()
            return True
        return False

    def _method_action(self, method: object, up: bool) -> None:
        if isinstance(method, str) and method:
            self.find.setText(method)
            self.show_tree((method, up))
            self.show_tab("calls")
            self.tree.setFocus()

    def _tree_menu(self, pos) -> None:
        method = self._tree_method()
        if method:
            self._menu_for(method, self.tree.viewport().mapToGlobal(pos))

    def _method_menu(self, table: Table, pos) -> None:
        method = table.data_at(table.indexAt(pos).row()) if table.indexAt(pos).isValid() else None
        if isinstance(method, str):
            self._menu_for(method, table.viewport().mapToGlobal(pos))

    def _menu_for(self, method: str, where) -> None:
        menu = QMenu(self)
        menu.addAction("What it calls", lambda: self._method_action(method, False))
        menu.addAction("Who calls it", lambda: self._method_action(method, True))
        menu.addSeparator()
        menu.addAction("Copy the method's name", lambda: self._copy(method, "Copied the name"))
        menu.addAction("Copy what it calls, as text for an LLM", lambda: self._copy_focus(method, False))
        menu.addAction("Copy who calls it, as text for an LLM", lambda: self._copy_focus(method, True))
        menu.exec(where)

    def _copy_focus(self, method: str, up: bool) -> None:
        p, name, thread, windows = self.profile, self.name_, self.thread, self.windows
        if p is not None:
            self._call(
                lambda: spark.focus_report(p, name, method, up, thread, windows),
                lambda text: self._copy(text, f"Copied {text.count(chr(10)) + 1} lines"),
            )

    def _mod_methods(self, mod: object) -> None:
        if isinstance(mod, str):
            self.methods_filter.setText(mod.replace(" (package)", "?"))
            self.show_tab("methods")

    # --- entities

    def _chunk_menu(self, pos) -> None:
        index = self.chunks.indexAt(pos)
        if index.isValid():
            where = self.chunks.data_at(index.row())
            menu = QMenu(self)
            menu.addAction("Copy a /tp command to this chunk", lambda: self._copy_tp(where))
            menu.exec(self.chunks.viewport().mapToGlobal(pos))

    def _copy_tp(self, where: object) -> None:
        if isinstance(where, tuple):
            self._copy(f"/tp {where[0]} ~ {where[1]}", f"Copied /tp {where[0]} ~ {where[1]}")

    # --- the text report tab

    def _view(self) -> Callable[[], str]:
        name, method, up, thread, windows = self.name_, self.method, self.up, self.thread, self.windows
        pair = self._pair()
        if pair is not None:
            (a, a_name), (b, b_name) = pair
            return lambda: spark.heap_compare(a, a_name, b, b_name)
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
        self._enable_report_buttons()
        where = [self.name_]
        if self.profile is not None and self.profile.threads:
            where.append(self.thread or spark.pick_thread(self.profile, None).name.lower())
        if self.windows:
            where.append(f"minute {', '.join(map(str, self.windows))}")
        if self.heap is not None:
            view = f"compared with {self.other[1]}" if self.other else "heap summary"
        elif self.method is None:
            view = "summary"
        else:
            view = f"{'callers of' if self.up else 'calls of'} {self.method}"
        self._say(f"{' · '.join(where)}   ·   report: {view}")

    def _go(self, **changes: object) -> None:
        """Switch the report to another view, remembering this one for Esc."""
        before = (self.method, self.up, self.windows, self.report.row())
        for key, value in changes.items():
            setattr(self, key, value)

        def shown(text: str) -> None:
            self.history.append(before)
            self._put(text, 0)
            if "windows" in changes:
                self._refresh_digest()

        def failed() -> None:
            self.method, self.up, self.windows, _ = before

        self._call(self._view(), shown, failed)

    def _enable_report_buttons(self) -> None:
        self.back_button.setEnabled(bool(self.history))
        self.summary_button.setEnabled(self.method is not None)

    def back(self) -> None:
        if not self.history:
            return
        windows = self.windows
        self.method, self.up, self.windows, row = self.history.pop()
        self._call(self._view(), lambda text: self._put(text, row))
        if self.windows != windows:
            self._refresh_digest()

    def summary(self) -> None:
        """Straight back to the summary (minute kept)."""
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

    def tree_report(self, up: bool) -> None:
        if self.profile is None:
            return
        method = method_on_line(self.report.line())
        if not method:
            self._say("Put the cursor on a line with a method first", True)
            return
        self._go(method=method, up=up)

    def _copy(self, text: str, said: str) -> None:
        QApplication.clipboard().setText(text)
        self._say(said)

    def copy(self) -> None:
        if self.text:
            self._copy(self.text, f"Copied the report, {self.text.count(chr(10)) + 1} lines")

    def _key(self, key: int, text: str) -> bool:
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.dig()
            return True
        k = self.letters()
        actions = {
            k["spark_calls"]: lambda: self.tree_report(False),
            k["spark_callers"]: lambda: self.tree_report(True),
            k["spark_summary"]: self.summary,
            k["spark_copy"]: self.copy,
        }
        if text in actions:
            actions[text]()
            return True
        if key == Qt.Key.Key_Backspace:
            self.back()
            return True
        return False

    def keyPressEvent(self, event) -> None:
        """Keys no child took (focus on a button or the tabs): Esc steps back in the call tree, and on the
        text report its keys still work. Nothing here closes the window."""
        key = self.tab_keys[self.tabs.currentIndex()] if self.tabs.currentIndex() >= 0 else ""
        plain = _plain(event)
        if event.key() == Qt.Key.Key_Escape and key == "calls" and self.focus is not None:
            self.show_tree(None)
        elif event.key() == Qt.Key.Key_Escape and key == "report":
            self.back()
        elif not (key == "report" and plain and self._key(event.key(), event.text())):
            super().keyPressEvent(event)
