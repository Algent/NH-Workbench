"""Desktop UI on Qt (PySide6), a proof of concept: the repo tabs, the repo menu, fetch and pull.

The git, GitHub and pack work is the code the terminal UI runs. It runs on an asyncio loop in a background
thread; results come back to the Qt thread through a signal. Cells and the detail pane reuse the Rich text the
terminal UI builds, turned into HTML, so both UIs show the same thing from one place.
"""

import asyncio
import concurrent.futures
import html
import io
import logging
import sys
import threading
import webbrowser
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QAbstractTextDocumentLayout, QAction, QIcon, QKeySequence, QPalette, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabBar,
    QTableView,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from rich.console import Console, RenderableType
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from . import config, github, ops, pack, proc
from .config import Config
from .models import PackInfo, RepoState
from .tui import HEADER_SORT, REPO_COLUMNS, SORTS, VIEWS, repo_actions, repo_cells, repo_detail

log = logging.getLogger(__name__)

HTML_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2
REPO_VIEWS = [key for key, (_, keep) in VIEWS.items() if keep is not None]  # the PR, pack and tools tabs come later
DONE_ACTIONS = {"fetch", "pull", "main", "ide", "web", "folder"}  # repo menu entries this preview handles

# ANSI colors the Rich styles map to, picked to read well on a light or a dark window.
_DARK = (
    [(60, 60, 60), (239, 83, 80), (102, 187, 106), (255, 202, 40), (66, 165, 245), (206, 147, 216), (77, 208, 225)]
    + [(200, 200, 200)],
    [(130, 130, 130), (255, 138, 128), (165, 214, 167), (255, 224, 130), (144, 202, 249), (225, 190, 231)]
    + [(128, 222, 234), (255, 255, 255)],
)
_LIGHT = (
    [(0, 0, 0), (198, 40, 40), (46, 125, 50), (166, 110, 0), (21, 101, 192), (142, 36, 170), (0, 131, 143)]
    + [(90, 90, 90)],
    [(100, 100, 100), (229, 57, 53), (67, 160, 71), (190, 130, 0), (30, 136, 229), (171, 71, 188), (0, 151, 167)]
    + [(40, 40, 40)],
)
_MONO = "Consolas, 'Cascadia Mono', Menlo, 'DejaVu Sans Mono', monospace"


def theme_for(palette: QPalette) -> TerminalTheme:
    bg, fg = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text)
    normal, bright = _DARK if bg.lightness() < 128 else _LIGHT
    return TerminalTheme(bg.getRgb()[:3], fg.getRgb()[:3], normal, bright)


_CELLS = Console(width=1000, color_system="truecolor", file=io.StringIO(), legacy_windows=False)


def _blend(a: tuple[int, int, int], b: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*((x + y) // 2 for x, y in zip(a, b, strict=True)))


def text_html(text: Text, theme: TerminalTheme) -> str:
    """One line of Rich text as Qt rich text (colors, bold, italic, dim)."""
    parts = []
    for seg in text.render(_CELLS):
        chunk = html.escape(seg.text).replace(" ", "&nbsp;")
        style = seg.style
        if not style or not chunk:
            parts.append(chunk)
            continue
        css = []
        fg = style.color.get_truecolor(theme) if style.color and not style.color.is_default else theme.foreground_color
        if style.dim:
            css.append(f"color:{_blend(tuple(fg), tuple(theme.background_color))}")
        elif style.color and not style.color.is_default:
            css.append(f"color:{fg.hex}")
        if style.bold:
            css.append("font-weight:600")
        if style.italic:
            css.append("font-style:italic")
        if style.strike:
            css.append("text-decoration:line-through")
        parts.append(f'<span style="{";".join(css)}">{chunk}</span>' if css else chunk)
    return "".join(parts)


def rich_html(renderable: RenderableType, theme: TerminalTheme, width: int) -> str:
    """A Rich renderable (tables included) as preformatted HTML, for the detail pane."""
    console = Console(record=True, width=width, file=io.StringIO(), color_system="truecolor", legacy_windows=False)
    console.print(renderable)
    code_format = f'<pre style="font-family:{_MONO}; color:{{foreground}}">{{code}}</pre>'
    return console.export_html(theme=theme, inline_styles=True, code_format=code_format)


# --- the work, without any widget ---------------------------------------------


class Workbench:
    """Repo states, PR and pack data, and the batches that update them.

    What the terminal UI's App keeps besides its widgets. Runs on the asyncio loop; the callbacks are called
    from there, so a UI has to bring them over to its own thread.
    """

    def __init__(
        self,
        cfg: Config,
        on_repo: Callable[[RepoState], None],
        on_change: Callable[[list[RepoState]], None],
        on_progress: Callable[[str, int, int], None],
        notify: Callable[[str, bool], None],
    ) -> None:
        self.cfg = cfg
        self.on_repo, self.on_change, self.on_progress, self.notify = on_repo, on_change, on_progress, notify
        self.states: dict[str, RepoState] = {}
        self.prs = github.PrData()
        self.pack_info: PackInfo | None = None
        self.busy = ""

    def _changed(self) -> None:
        self.on_change(sorted(self.states.values(), key=ops.by_repo))

    async def load(self, fetch: bool = False) -> None:
        worker = ops.fetch_repo if fetch else ops.scan_repo
        if not await self.batch("Fetching" if fetch else "Scanning", lambda p: worker(self.cfg, p), reset=True):
            return
        await asyncio.gather(self._load_prs(), self._load_pack())  # independent: neither holds the other up

    async def batch(
        self,
        label: str,
        worker: Callable[[Path], Awaitable[RepoState]],
        paths: list[Path] | None = None,
        reset: bool = False,
    ) -> bool:
        if self.busy:
            self.notify(f"{self.busy} is still running", True)
            return False
        self.busy = label
        try:
            if paths is None:
                paths = ops.discover(self.cfg)
            if reset:
                keep = {str(p) for p in paths}
                self.states = {k: v for k, v in self.states.items() if k in keep}
                self._changed()
            done = 0

            def on_done(st: RepoState) -> None:
                nonlocal done
                done += 1
                github.attach([st], self.prs)
                old = self.states.get(str(st.path))
                if old:  # keep the pack columns until they are recomputed after the batch
                    st.daily, st.release, st.release_ahead = old.daily, old.release, old.release_ahead
                    st.daily_ahead, st.release_commits = old.daily_ahead, old.release_commits
                    st.release_delta = old.release_delta
                self.states[str(st.path)] = st
                self.on_repo(st)
                self.on_progress(label, done, len(paths))

            await ops.run_all(paths, worker, self.cfg.concurrency, on_done)
        finally:
            self.busy = ""
            self.on_progress("", 0, 0)
        await self._relink(paths)
        self._changed()
        return True

    async def _load_prs(self) -> None:
        data = await github.fetch_prs(self.states.values())
        if data.error:
            self.notify(f"PR status unavailable: {data.error}", True)
        if data.prs or data.canonical or not data.error:
            self.prs = data
            github.attach(self.states.values(), data)
            await self._relink()
            self._changed()

    async def _load_pack(self) -> None:
        try:
            self.pack_info = await pack.load(self.cfg)
        except pack.PackError as e:
            self.notify(str(e), True)
        await self._relink()
        self._changed()

    async def _relink(self, paths: list[Path] | None = None) -> None:
        if not self.pack_info:
            return
        pack.link_local(self.pack_info.entries, self.states.values())
        keys = self.states if paths is None else [str(p) for p in paths]
        states = [self.states[k] for k in keys if k in self.states]
        branch = pack.release_branch(self.pack_info.release) if self.pack_info.release else ""
        await ops.pack_progress(states, branch, self.cfg.concurrency)


class AsyncLoop:
    """An asyncio loop in a background thread, so git and gh calls never freeze the window."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()  # a proactor loop on Windows: subprocesses work
        self.thread = threading.Thread(target=self.loop.run_forever, name="nhwb-async", daemon=True)
        self.thread.start()

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


class Bridge(QObject):
    """Runs functions on the Qt thread when asked from any thread."""

    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._run)  # a slot of an object living on the Qt thread: queued from other threads

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        fn()


# --- the table ------------------------------------------------------------------


class RepoModel(QAbstractTableModel):
    def __init__(self, theme: TerminalTheme) -> None:
        super().__init__()
        self.theme = theme
        self.rows: list[RepoState] = []
        self._cells: list[list[tuple[str, str]]] = []  # (plain, html) per column

    def _render(self, st: RepoState) -> list[tuple[str, str]]:
        return [(t.plain, text_html(t, self.theme)) for t in repo_cells(st)]

    def set_rows(self, states: list[RepoState]) -> None:
        self.beginResetModel()
        self.rows = list(states)
        self._cells = [self._render(st) for st in self.rows]
        self.endResetModel()

    def update_row(self, st: RepoState) -> None:
        for i, old in enumerate(self.rows):
            if old.path == st.path:
                self.rows[i], self._cells[i] = st, self._render(st)
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(REPO_COLUMNS) - 1))
                return
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
        self.rows.append(st)
        self._cells.append(self._render(st))
        self.endInsertRows()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(REPO_COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return REPO_COLUMNS[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        st, column = self.rows[index.row()], REPO_COLUMNS[index.column()]
        plain, rich = self._cells[index.row()][index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return plain
        if role == HTML_ROLE:
            return rich
        if role == SORT_ROLE:
            key = HEADER_SORT.get(column)
            return SORTS[key][1](st) if key else plain.lower()
        if role == Qt.ItemDataRole.UserRole:
            return st
        return None


class RepoFilter(QSortFilterProxyModel):
    """The repo tabs (All, Off main, ...) and the name filter."""

    def __init__(self) -> None:
        super().__init__()
        self.view = "all"
        self.needle = ""

    def set_view(self, view: str, needle: str) -> None:
        self.beginFilterChange()
        self.view, self.needle = view, needle.lower()
        self.endFilterChange()

    def filterAcceptsRow(self, row: int, parent: QModelIndex | QPersistentModelIndex) -> bool:
        st = self.sourceModel().rows[row]
        keep = VIEWS[self.view][1]
        return bool(keep and keep(st)) and self.needle in st.name.lower()

    def lessThan(self, left: QModelIndex | QPersistentModelIndex, right: QModelIndex | QPersistentModelIndex) -> bool:
        return left.data(SORT_ROLE) < right.data(SORT_ROLE)


class RichDelegate(QStyledItemDelegate):
    """Paints a cell's HTML, so one cell can hold several colors like in the terminal UI."""

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._docs: dict[tuple[str, str], QTextDocument] = {}

    def _doc(self, rich: str, option: QStyleOptionViewItem) -> QTextDocument:
        key = (rich, option.font.key())
        doc = self._docs.get(key)
        if doc is None:
            if len(self._docs) > 4000:
                self._docs.clear()
            doc = QTextDocument()
            doc.setDocumentMargin(0)
            doc.setDefaultFont(option.font)
            doc.setHtml(rich)
            self._docs[key] = doc
        return doc

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        doc = self._doc(index.data(HTML_ROLE) or "", option)
        rect = option.rect.adjusted(6, 0, -4, 0)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        group = QPalette.ColorGroup.Active  # the group the style painted the background with
        if not option.state & QStyle.StateFlag.State_Enabled:
            group = QPalette.ColorGroup.Disabled
        elif not option.state & QStyle.StateFlag.State_Active:
            group = QPalette.ColorGroup.Inactive
        ctx.palette.setColor(QPalette.ColorRole.Text, option.palette.color(group, role))
        ctx.clip = QRectF(0, 0, rect.width(), rect.height())
        painter.save()
        painter.translate(rect.left(), rect.top() + (rect.height() - doc.size().height()) / 2)
        painter.setClipRect(ctx.clip)
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:
        doc = self._doc(index.data(HTML_ROLE) or "", option)
        return QSize(int(doc.idealWidth()) + 14, int(doc.size().height()) + 8)


class KeyMenu(QMenu):
    """A menu whose entries also answer to their letter, like the terminal UI's menus."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.chosen: QAction | None = None

    def keyPressEvent(self, event) -> None:
        key = event.text().lower()
        for action in self.actions():
            if key and action.isEnabled() and action.shortcut().toString().lower() == key:
                self.chosen = action
                self.close()
                return
        super().keyPressEvent(event)


# --- the window -----------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.bridge = Bridge()
        self.loop = AsyncLoop()
        self.theme = theme_for(self.palette())
        self.wb = Workbench(
            cfg,
            on_repo=self._later(self._repo_loaded),
            on_change=self._later(self._all_loaded),
            on_progress=self._later(self._progress),
            notify=self._later(self._notify),
        )
        self.setWindowTitle(f"NH Workbench - {cfg.root}")
        icon = Path(__file__).resolve().parents[2] / "nh-workbench.ico"  # in a checkout, not in a wheel
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self._build()
        self.resize(1400, 820)
        self.run(self.wb.load())

    def _build(self) -> None:
        bar = QToolBar("Actions")
        bar.setMovable(False)
        self.addToolBar(bar)
        for label, keys, slot, tip in (
            ("Fetch all", "Ctrl+Shift+F", lambda: self._operate("Fetching", ops.fetch_repo), "Fetch every repo"),
            ("Pull safe", "Ctrl+Shift+P", lambda: self._operate("Pulling", ops.pull_repo), "Fast-forward main if safe"),
            ("Rescan", "F5", lambda: self.run(self.wb.load()), "Read every repo again"),
        ):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(keys))
            action.setToolTip(f"{tip} ({keys})")
            action.triggered.connect(slot)
            bar.addAction(action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name (Ctrl+F)")
        self.filter.setClearButtonEnabled(True)
        self.filter.setFixedWidth(260)
        self.filter.textChanged.connect(lambda _: self._apply_view())
        bar.addWidget(self.filter)
        find = QAction(self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: (self.filter.setFocus(), self.filter.selectAll()))
        self.addAction(find)

        self.tabs = QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        for key in REPO_VIEWS:
            self.tabs.addTab(VIEWS[key][0])
        self.tabs.currentChanged.connect(lambda _: self._apply_view())

        self.model = RepoModel(self.theme)
        self.proxy = RepoFilter()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setItemDelegate(RichDelegate(self.table))
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 10)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        menu_at = self.table.customContextMenuRequested
        menu_at.connect(lambda pos: self._repo_menu(self.table.viewport().mapToGlobal(pos)))
        self.table.activated.connect(lambda _: self._repo_menu())  # Enter or double click
        self.table.selectionModel().currentRowChanged.connect(lambda *_: self._update_detail())

        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setPlaceholderText("Select a repo")

        top = QWidget()
        layout = QVBoxLayout(top)
        layout.setContentsMargins(6, 4, 6, 0)
        layout.setSpacing(2)
        layout.addWidget(self.tabs)
        layout.addWidget(self.table)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

        self.message = QLabel()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setFormat("%v/%m")
        self.progress.hide()
        self.statusBar().addWidget(self.message, 1)
        self.statusBar().addPermanentWidget(self.progress)

    # --- plumbing between the two threads

    def _later(self, fn: Callable) -> Callable:
        """Wrap fn so that calling it from the asyncio thread runs it on the Qt thread."""
        return lambda *args: self.bridge.call.emit(lambda: fn(*args))

    def run(self, coro: Coroutine) -> None:
        future = self.loop.submit(coro)

        def done(f: concurrent.futures.Future) -> None:
            if not f.cancelled() and f.exception():
                log.error("background task failed", exc_info=f.exception())
                self.bridge.call.emit(lambda: self._notify(f"Failed: {f.exception()}", True))

        future.add_done_callback(done)

    def closeEvent(self, event) -> None:
        self.loop.stop()
        super().closeEvent(event)

    # --- results coming in

    def _repo_loaded(self, st: RepoState) -> None:
        self.model.update_row(st)
        self._count_tabs()
        if self._selected() and self._selected().path == st.path:
            self._update_detail()

    def _all_loaded(self, states: list[RepoState]) -> None:
        selected = self._selected()
        self.model.set_rows(states)
        self._count_tabs()
        if selected:
            self._select(selected.path)
        elif self.proxy.rowCount():
            self.table.selectRow(0)
        self.table.resizeColumnsToContents()
        self._update_detail()

    def _progress(self, label: str, done: int, total: int) -> None:
        if not label:
            self.progress.hide()
            if self.message.text().endswith("..."):
                self.message.setText(f"{len(self.model.rows)} repos")
            return
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.progress.show()
        self.message.setText(f"{label}...")

    def _notify(self, text: str, warning: bool = False) -> None:
        self.message.setText(text)
        self.statusBar().setStyleSheet("color: #c62828;" if warning else "")

    # --- view

    def _view(self) -> str:
        return REPO_VIEWS[max(self.tabs.currentIndex(), 0)]

    def _apply_view(self) -> None:
        self.proxy.set_view(self._view(), self.filter.text())
        if not self._selected() and self.proxy.rowCount():
            self.table.selectRow(0)
        self._update_detail()

    def _count_tabs(self) -> None:
        for i, key in enumerate(REPO_VIEWS):
            label, keep = VIEWS[key]
            n = sum(1 for st in self.model.rows if keep and keep(st))
            self.tabs.setTabText(i, f"{label}  {n}")

    def _selected(self) -> RepoState | None:
        index = self.table.currentIndex()
        return index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None

    def _select(self, path: Path) -> None:
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if index.data(Qt.ItemDataRole.UserRole).path == path:
                self.table.setCurrentIndex(index)
                self.table.selectRow(row)
                return

    def _update_detail(self) -> None:
        st = self._selected()
        if not st:
            self.detail.clear()
            return
        width = max(60, self.detail.viewport().width() // max(1, self.detail.fontMetrics().horizontalAdvance("M")))
        self.detail.setHtml(rich_html(repo_detail(st, self.wb.pack_info), self.theme, width))

    # --- actions

    def _operate(self, label: str, worker: Callable[[Config, Path], Awaitable[RepoState]], st=None) -> None:
        paths = [st.path] if st else None
        self.run(self.wb.batch(label, lambda p: worker(self.cfg, p), paths))

    def _repo_menu(self, where=None) -> None:
        st = self._selected()
        if not st or st.error:
            return
        menu = KeyMenu(self)
        menu.addSection(st.name)
        for key, action_id, label, reason in repo_actions(st, self.cfg.ide_off):
            if action_id not in DONE_ACTIONS:
                reason = "not in the desktop preview yet"
            action = menu.addAction(f"{label}  ({reason})" if reason else label)
            action.setShortcut(QKeySequence(key))
            action.setEnabled(reason is None)
            action.setData(action_id)
        if where is None:
            rect = self.table.visualRect(self.table.currentIndex())
            where = self.table.viewport().mapToGlobal(rect.bottomLeft())
        chosen = menu.exec(where) or menu.chosen
        if chosen:
            self._repo_action(chosen.data(), st)

    def _repo_action(self, action_id: str, st: RepoState) -> None:
        if action_id == "fetch":
            self._operate("Fetching", ops.fetch_repo, st)
        elif action_id == "pull":
            self._operate("Pulling", ops.pull_repo, st)
        elif action_id == "main":
            self.run(self.wb.batch("Switching", lambda p: ops.to_main(self.cfg, st), [st.path]))
        elif action_id == "web":
            pr = st.current.pr if st.current else None
            url = pr.url if pr else (f"https://github.com/{st.slug}" if st.slug else None)
            if url:
                webbrowser.open(url)
        elif action_id == "folder":
            proc.open_path(st.path)
        elif action_id == "ide":
            ide = config.find_ide(self.cfg)
            if not ide or not config.runnable(ide):
                self._notify("No IntelliJ launcher found: set ide_command in the config file", True)
                return
            try:
                proc.launch(ide, str(st.path))
            except OSError as e:
                self._notify(f"Could not start {ide}: {e}", True)
                return
            self._notify(f"Opening {st.name}")


def run(cfg: Config) -> int:
    if sys.platform == "win32":  # its own taskbar entry and icon, not Python's
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NHWorkbench.Desktop")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("NH Workbench")
    app.setStyle("Fusion")  # looks the same on Windows, Linux and macOS, follows the system's dark mode
    if not cfg.root.is_dir():
        folder = QFileDialog.getExistingDirectory(None, "Folder with your checkouts", str(Path.home()))
        if not folder:
            return 1
        cfg.root = Path(folder)
    window = MainWindow(cfg)
    window.show()
    return app.exec()


def main() -> None:
    """Entry point of the nh-workbench-desktop launcher (no console window on Windows)."""
    try:
        cfg = config.load()
    except config.ConfigError as e:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "NH Workbench", str(e))
        app.quit()
        raise SystemExit(2) from e
    raise SystemExit(run(cfg))
