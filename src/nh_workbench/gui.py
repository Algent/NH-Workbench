"""Desktop UI on Qt (PySide6), a proof of concept: the repo tabs, the Pack tab, the menus, fetch, pull and clone.

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
    QStackedWidget,
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
from rich.segment import Segment
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from . import config, github, ops, pack, proc, render
from .config import Config
from .models import PackEntry, PackInfo, RepoState
from .tui import (
    HEADER_SORT,
    PACK_COLUMNS,
    REPO_COLUMNS,
    SORTS,
    VIEWS,
    action_shown,
    pack_detail,
    repo_actions,
    repo_cells,
    repo_detail,
)

log = logging.getLogger(__name__)

RESULT = REPO_COLUMNS.index("Result")
HTML_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2
REPO_VIEWS = [key for key, (_, keep) in VIEWS.items() if keep is not None]
VIEW_KEYS = [*REPO_VIEWS, "pack"]  # the PR and Tools tabs come later
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
    segments = list(text.render(_CELLS))
    if not text.spans and text.style:  # render() only applies the base style when there are spans
        segments = [Segment(text.plain, _CELLS.get_style(text.style))]
    for seg in segments:
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
        on_change: Callable[[list[RepoState], list[PackEntry]], None],
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
        entries = list(self.pack_info.entries) if self.pack_info else []
        self.on_change(sorted(self.states.values(), key=ops.by_repo), entries)

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

    async def _load_pack(self, force: bool = False) -> None:
        try:
            self.pack_info = await pack.load(self.cfg, force)
        except pack.PackError as e:
            self.notify(str(e), True)
        await self._relink()
        self._changed()

    async def refresh_pack(self) -> None:
        """Download the pack data again instead of using the copy cached for an hour."""
        try:
            await self._load_pack(force=True)
        finally:
            self.on_progress("", 0, 0)

    async def clone(self, entry: PackEntry) -> None:
        """Clone a pack mod into the root, then scan it so it shows up with the other checkouts."""
        try:
            res = await pack.clone(entry, self.cfg.root, self.cfg.clone_protocol)
            if not res.ok:
                self.notify(f"{entry.name}: {res.message}", True)
                return
            job = pack.clone_job(entry, self.cfg.root, self.cfg.clone_protocol)
            await self.batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), [job.dest])
            self.notify(f"Cloned {entry.repo} into {job.dest}", False)
        finally:
            self.on_progress("", 0, 0)

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


# --- the tables -----------------------------------------------------------------


class RichModel(QAbstractTableModel):
    """Rows of objects shown as Rich text cells; subclasses say how to render, key and sort them."""

    columns: tuple[str, ...] = ()

    def __init__(self, theme: TerminalTheme) -> None:
        super().__init__()
        self.theme = theme
        self.headers = list(self.columns)
        self.rows: list = []
        self._cells: list[list[tuple[str, str]]] = []  # (plain, html) per column

    def cells(self, obj) -> list[Text]:
        raise NotImplementedError

    def key(self, obj) -> object:
        raise NotImplementedError

    def sort_key(self, obj, column: str, plain: str) -> object:
        return plain.lower()

    def _render(self, obj) -> list[tuple[str, str]]:
        return [(t.plain, text_html(t, self.theme)) for t in self.cells(obj)]

    def set_rows(self, rows: list) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self._cells = [self._render(obj) for obj in self.rows]
        self.endResetModel()

    def set_headers(self, labels: dict[str, str]) -> None:
        self.headers = [labels.get(c, c) for c in self.columns]
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.columns) - 1)

    def update_row(self, obj) -> None:
        key = self.key(obj)
        for i, old in enumerate(self.rows):
            if self.key(old) == key:
                self.rows[i], self._cells[i] = obj, self._render(obj)
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(self.columns) - 1))
                return
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
        self.rows.append(obj)
        self._cells.append(self._render(obj))
        self.endInsertRows()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        obj = self.rows[index.row()]
        plain, rich = self._cells[index.row()][index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return plain
        if role == HTML_ROLE:
            return rich
        if role == SORT_ROLE:
            return self.sort_key(obj, self.columns[index.column()], plain)
        if role == Qt.ItemDataRole.UserRole:
            return obj
        return None


class RepoModel(RichModel):
    columns = REPO_COLUMNS

    def __init__(self, theme: TerminalTheme) -> None:
        super().__init__(theme)
        self.pending: dict[Path, str] = {}  # repos a batch has not reached yet, and what it is doing

    def cells(self, st: RepoState) -> list[Text]:
        cells = repo_cells(st)
        if st.path in self.pending:
            cells[RESULT] = Text(f"{self.pending[st.path].lower()}...", "dim italic")
        return cells

    def key(self, st: RepoState) -> Path:
        return st.path

    def sort_key(self, st: RepoState, column: str, plain: str) -> object:
        key = HEADER_SORT.get(column)
        return SORTS[key][1](st) if key else plain.lower()

    def mark(self, paths: list[Path], label: str) -> None:
        """Show at once which repos a batch is about to work on."""
        self.pending = dict.fromkeys(paths, label)
        self._rerender()

    def unmark(self) -> None:
        if self.pending:
            self.pending = {}
            self._rerender()

    def _rerender(self) -> None:
        self._cells = [self._render(st) for st in self.rows]
        if self.rows:
            self.dataChanged.emit(self.index(0, RESULT), self.index(len(self.rows) - 1, RESULT))

    def update_row(self, st: RepoState) -> None:
        self.pending.pop(st.path, None)
        super().update_row(st)


class PackModel(RichModel):
    columns = PACK_COLUMNS

    def cells(self, entry: PackEntry) -> list[Text]:
        return render.pack_row(entry)

    def key(self, entry: PackEntry) -> str:
        return entry.name


class RichFilter(QSortFilterProxyModel):
    """Keeps the rows a tab and the name filter let through, and sorts by each model's own keys."""

    def __init__(self, accept: Callable[[object], bool]) -> None:
        super().__init__()
        self.accept = accept

    def refilter(self) -> None:
        self.beginFilterChange()
        self.endFilterChange()

    def filterAcceptsRow(self, row: int, parent: QModelIndex | QPersistentModelIndex) -> bool:
        return self.accept(self.sourceModel().rows[row])

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


class RichTable(QTableView):
    """A sortable, filterable table of a RichModel. Letter keys go to `on_letter`, like the TUI's keys."""

    def __init__(self, model: RichModel, accept: Callable[[object], bool], row_height: int) -> None:
        super().__init__()
        self.source = model
        self.proxy = RichFilter(accept)
        self.proxy.setSourceModel(model)
        self.on_letter: Callable[[str], bool] = lambda key: False
        self.setModel(self.proxy)
        self.setItemDelegate(RichDelegate(self))
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(row_height)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setResizeContentsPrecision(60)  # size columns from a sample: the pack has 250+ rows
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def current(self) -> object | None:
        index = self.currentIndex()
        return index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None

    def select_key(self, key: object) -> bool:
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if self.source.key(index.data(Qt.ItemDataRole.UserRole)) == key:
                self.setCurrentIndex(index)
                self.selectRow(row)
                return True
        return False

    def keyPressEvent(self, event) -> None:
        plain = event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)
        if plain and event.text().isalpha() and self.on_letter(event.text().lower()):
            return
        super().keyPressEvent(event)


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


def pack_actions(entry: PackEntry, cfg: Config) -> list[tuple[str, str, str, str | None]]:
    """(key, id, label, reason it is unavailable) for a pack mod, with the TUI's Pack tab keys."""
    job = pack.clone_job(entry, cfg.root, cfg.clone_protocol)
    no_clone = None
    if entry.local:
        no_clone = "already checked out"
    elif entry.closed:
        no_clone = "closed source"
    elif not job:
        no_clone = "source repository unknown"
    elif job.dest.exists():
        no_clone = f"{job.dest.name} already exists"
    where = f"{entry.repo} @ {entry.branch}" if entry.branch else entry.repo or entry.name
    on_web = "Open on GitHub" if entry.host == "github.com" and entry.repo else "Open web page"
    return [
        ("c", "clone", f"Clone {where}", no_clone),
        ("w", "web", on_web, None if entry.web_url else "no web page known"),
        ("e", "folder", "Open folder", None if entry.local else "not checked out"),
        ("o", "ide", "Open in IntelliJ", None if entry.local and not cfg.ide_off else "not checked out"),
        ("r", "refresh", "Download the pack data again", None),
    ]


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
        self._rescan()

    def _build(self) -> None:
        self.running = ""  # label of the batch in progress
        bar = QToolBar("Actions")
        bar.setMovable(False)
        self.addToolBar(bar)
        # Named like the terminal UI's actions: action_shown() decides on which tabs each one appears.
        self.tab_actions: dict[str, QAction] = {}
        for action_id, label, keys, slot, tip in (
            (
                "fetch",
                "Fetch all",
                "Ctrl+Shift+F",
                lambda: self._operate("Fetching", ops.fetch_repo),
                "Fetch every repo",
            ),
            (
                "pull",
                "Pull safe",
                "Ctrl+Shift+P",
                lambda: self._operate("Pulling", ops.pull_repo),
                "Fast-forward main if safe",
            ),
            ("rescan", "Rescan", "F5", self._rescan, "Read every repo again"),
            ("clone", "Clone", "Ctrl+Shift+C", lambda: self._act("clone"), "Clone the selected mod (c)"),
            ("open_web", "Open in browser", "Ctrl+Shift+G", lambda: self._act("web"), "PR or GitHub page of the line"),
            ("open_ide", "Open in IntelliJ", "Ctrl+Shift+O", lambda: self._act("ide"), "Open the checkout of the line"),
        ):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(keys))
            action.setToolTip(f"{tip} ({keys})")
            action.triggered.connect(slot)
            bar.addAction(action)
            self.tab_actions[action_id] = action
        self.batch_actions = [self.tab_actions[k] for k in ("fetch", "pull", "rescan", "clone")]
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name (Ctrl+F)")
        self.filter.setClearButtonEnabled(True)
        self.filter.setFixedWidth(260)
        self.filter.textChanged.connect(lambda _: self._apply_view())
        self.filter_action = bar.addWidget(self.filter)
        find = QAction(self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: (self.filter.setFocus(), self.filter.selectAll()))
        self.addAction(find)

        self.tabs = QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        for key in VIEW_KEYS:
            self.tabs.addTab(VIEWS[key][0])
        self.tabs.currentChanged.connect(lambda _: self._apply_view())

        height = self.fontMetrics().height() + 10
        self.repos = RichTable(RepoModel(self.theme), self._repo_shown, height)
        self.pack = RichTable(PackModel(self.theme), self._entry_shown, height)
        self.pack.on_letter = self._pack_letter
        self.stack = QStackedWidget()
        for table in (self.repos, self.pack):
            table.customContextMenuRequested.connect(lambda pos, t=table: self._menu(t.viewport().mapToGlobal(pos)))
            table.activated.connect(lambda _: self._menu())  # Enter or double click
            table.selectionModel().currentRowChanged.connect(lambda *_: self._update_detail())
            self.stack.addWidget(table)

        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setPlaceholderText("Select a line")

        top = QWidget()
        layout = QVBoxLayout(top)
        layout.setContentsMargins(6, 4, 6, 0)
        layout.setSpacing(2)
        layout.addWidget(self.tabs)
        layout.addWidget(self.stack)
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
        self._show_actions()

    @property
    def model(self) -> RepoModel:
        return self.repos.source

    @property
    def table(self) -> RichTable:
        return self.pack if self._view() == "pack" else self.repos

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

    def _all_loaded(self, states: list[RepoState], entries: list[PackEntry]) -> None:
        for table, rows in ((self.repos, states), (self.pack, entries)):
            current = table.current()
            first = not table.source.rows
            table.source.set_rows(rows)
            if not (current is not None and table.select_key(table.source.key(current))) and table.proxy.rowCount():
                table.selectRow(0)
            if first and rows:
                table.resizeColumnsToContents()
        self.pack.source.set_headers(render.pack_labels(self.wb.pack_info))
        self._count_tabs()
        self._update_detail()

    def _progress(self, label: str, done: int, total: int) -> None:
        if not label:
            self._busy(False)
            self.model.unmark()
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
        return VIEW_KEYS[max(self.tabs.currentIndex(), 0)]

    def _needle(self) -> str:
        return self.filter.text().lower()

    def _repo_shown(self, st: RepoState) -> bool:
        keep = VIEWS.get(self._view(), ("", None))[1]
        return bool(keep and keep(st)) and self._needle() in st.name.lower()

    def _entry_shown(self, entry: PackEntry) -> bool:
        return self._needle() in entry.name.lower()

    def _show_actions(self) -> None:
        """Keep the toolbar to what works on the current tab, by the terminal UI's own rule."""
        view = self._view()
        for action_id, action in self.tab_actions.items():
            action.setVisible(action_shown(action_id, view, self.cfg.ide_off))
        self.filter_action.setVisible(action_shown("filter", view))

    def _apply_view(self) -> None:
        self._show_actions()
        table = self.table
        self.stack.setCurrentWidget(table)
        table.proxy.refilter()
        if table.current() is None and table.proxy.rowCount():
            table.selectRow(0)
        self._update_detail()

    def _count_tabs(self) -> None:
        for i, key in enumerate(VIEW_KEYS):
            label, keep = VIEWS[key]
            if key == "pack":
                n = len(self.pack.source.rows)
            else:
                n = sum(1 for st in self.model.rows if keep and keep(st))
            self.tabs.setTabText(i, f"{label}  {n}")

    def _selected(self) -> RepoState | None:
        return self.repos.current() if self._view() != "pack" else None

    def _selected_entry(self) -> PackEntry | None:
        return self.pack.current() if self._view() == "pack" else None

    def _update_detail(self) -> None:
        st, entry = self._selected(), self._selected_entry()
        if st:
            content = repo_detail(st, self.wb.pack_info)
        elif entry and self.wb.pack_info:
            content = pack_detail(entry, self.wb.pack_info)
        else:
            self.detail.clear()
            return
        width = max(60, self.detail.viewport().width() // max(1, self.detail.fontMetrics().horizontalAdvance("M")))
        self.detail.setHtml(rich_html(content, self.theme, width))

    # --- actions

    def _busy(self, busy: bool) -> None:
        if not busy:
            self.running = ""
        for action in self.batch_actions:
            action.setEnabled(not busy)

    def _start(self, label: str, paths: list[Path] | None) -> list[Path] | None:
        """Show the batch right away: a fetch only reports its first repo after a network round trip.

        Returns the paths to work on, or None when another batch is still running.
        """
        if self.running:  # set here, not in the worker, so two quick clicks cannot both get through
            self._notify(f"{self.running} is still running", True)
            return None
        paths = paths if paths is not None else ops.discover(self.cfg)
        self.running = label
        self._busy(True)
        self.model.mark(paths, label)
        self._progress(label, 0, len(paths))
        return paths

    def _operate(self, label: str, worker: Callable[[Config, Path], Awaitable[RepoState]], st=None) -> None:
        paths = self._start(label, [st.path] if st else None)
        if paths is not None:
            self.run(self.wb.batch(label, lambda p: worker(self.cfg, p), paths))

    def _rescan(self) -> None:
        if self._start("Scanning", None) is not None:
            self.run(self.wb.load())

    def _menu(self, where=None) -> None:
        """The menu of the selected line: Enter, double click or right click."""
        st, entry = self._selected(), self._selected_entry()
        if st and not st.error:
            actions = [
                (key, id_, label, reason if id_ in DONE_ACTIONS else "not in the desktop preview yet")
                for key, id_, label, reason in repo_actions(st, self.cfg.ide_off)
            ]
            chosen = self._ask(st.name, actions, where)
            if chosen:
                self._repo_action(chosen, st)
        elif entry:
            chosen = self._ask(entry.name, pack_actions(entry, self.cfg), where)
            if chosen:
                self._pack_action(chosen, entry)

    def _ask(self, title: str, actions: list[tuple[str, str, str, str | None]], where=None) -> str | None:
        menu = KeyMenu(self)
        menu.addSection(title)
        for key, action_id, label, reason in actions:
            action = menu.addAction(f"{label}  ({reason})" if reason else label)
            action.setShortcut(QKeySequence(key))
            action.setEnabled(reason is None)
            action.setData(action_id)
        if where is None:
            rect = self.table.visualRect(self.table.currentIndex())
            where = self.table.viewport().mapToGlobal(rect.bottomLeft())
        chosen = menu.exec(where) or menu.chosen
        return chosen.data() if chosen else None

    def _pack_letter(self, key: str) -> bool:
        """c, w, ... on the Pack tab act on the selected mod at once, as in the terminal UI."""
        entry = self._selected_entry()
        action_id = next((i for k, i, _, _ in pack_actions(entry, self.cfg) if k == key), None) if entry else None
        if action_id:
            self._act(action_id)
        return action_id is not None

    def _act(self, action_id: str) -> None:
        """Run a menu action on the selected line, from the toolbar or a letter key; say why when it cannot."""
        st, entry = self._selected(), self._selected_entry()
        if entry:
            reason = next((r for _, i, _, r in pack_actions(entry, self.cfg) if i == action_id), "not for pack mods")
            if reason:
                self._notify(f"{entry.name}: {reason}", True)
            else:
                self._pack_action(action_id, entry)
        elif st:
            reason = next((r for _, i, _, r in repo_actions(st, self.cfg.ide_off) if i == action_id), None)
            if reason:
                self._notify(f"{st.name}: {reason}", True)
            else:
                self._repo_action(action_id, st)

    def _pack_action(self, action_id: str, entry: PackEntry) -> None:
        if action_id == "clone":
            if self._start(f"Cloning {entry.repo}", []) is not None:
                self.run(self.wb.clone(entry))
        elif action_id == "web":
            if entry.web_url:
                webbrowser.open(entry.web_url)
        elif action_id == "refresh":
            if self._start("Downloading the pack data", []) is not None:
                self.run(self.wb.refresh_pack())
        elif entry.local:  # open folder, open in the IDE: the same as for the checkout
            self._repo_action(action_id, entry.local)

    def _repo_action(self, action_id: str, st: RepoState) -> None:
        if action_id == "fetch":
            self._operate("Fetching", ops.fetch_repo, st)
        elif action_id == "pull":
            self._operate("Pulling", ops.pull_repo, st)
        elif action_id == "main":
            if self._start("Switching", [st.path]) is not None:
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
