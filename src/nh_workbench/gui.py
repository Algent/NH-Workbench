"""Desktop UI on Qt (PySide6): the terminal UI's tabs, actions and keys in a window.

The git, GitHub and pack work is the code the terminal UI runs. It runs on an asyncio loop in a background
thread; results come back to the Qt thread through a signal. Cells and the detail pane reuse the Rich text the
terminal UI builds, turned into HTML, so both UIs show the same thing from one place.
"""

import asyncio
import concurrent.futures
import contextlib
import html
import io
import logging
import logging.handlers
import sys
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QAction,
    QColor,
    QIcon,
    QKeySequence,
    QPalette,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from rich.console import Console, Group, RenderableType
from rich.segment import Segment
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from . import config, github, gui_theme, ops, pack, proc, render, skill, tui
from .config import Config
from .gui_settings import open_folder
from .models import CloneJob, PackEntry, PackInfo, RepoState, ReviewItem
from .tui import (
    HEADER_SORT,
    PACK_COLUMNS,
    REPO_COLUMNS,
    SORTS,
    TAB_GROUP_STARTS,
    TOOL_COLUMNS,
    TOOLS,
    VIEWS,
    action_shown,
    local_branch,
    mine_detail,
    org_detail,
    org_mark,
    org_shown,
    pack_detail,
    pr_matches,
    repo_actions,
    repo_cells,
    repo_detail,
    repo_for,
    review_detail,
    tool_status,
)

log = logging.getLogger(__name__)

RESULT = REPO_COLUMNS.index("Result")
HTML_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2
# Letters for toolbar actions on the tabs where they show, as in the terminal UI.
# The terminal UI's keys, each to the toolbar action it is on a tab: the first one shown there wins.
LETTERS = [
    ("f", "fetch"),
    ("p", "pull"),
    ("m", "to_main"),
    ("d", "cleanup"),
    ("r", "rescan"),
    ("r", "reload_prs"),
    ("r", "reload_org"),
    ("c", "clone"),
    ("c", "checkout"),
    ("t", "toggle_team"),
    ("t", "toggle_drafts"),
    ("v", "pr_filters"),
    ("w", "open_web"),
    ("o", "open_ide"),
]
ASC, DESC = Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder
PR_SORT_COLUMNS = {"updated": ("Updated", ASC), "small": ("Size", ASC), "large": ("Size", DESC), "repo": ("Repo", ASC)}
REPO_VIEWS = [key for key, (_, keep) in VIEWS.items() if keep is not None]
PR_TABS = ("mine", "reviews", "org")
FAILURES_SHOWN = 5  # failures told one by one before the summary, as the terminal UI's notifications
HISTORY_SIZE = 300  # messages kept for the status bar's history
VIEW_KEYS = list(VIEWS)  # the same tabs as the terminal UI, in the same order

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


def theme_for(palette: QPalette, name: str = "") -> TerminalTheme:
    """The colors Rich text is shown in. A Textual theme's own red, green and yellow, as the TUI shows them."""
    bg, fg = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text)
    normal, bright = (list(colors) for colors in (_DARK if bg.lightness() < 128 else _LIGHT))
    themed = gui_theme.colors(name)
    if themed:
        for index, key in ((1, "error"), (2, "success"), (3, "warning")):
            normal[index] = bright[index] = themed[key].rgb
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


def rich_html(renderable: RenderableType, theme: TerminalTheme, width: int, size: int = 0) -> str:
    """A Rich renderable (tables included) as preformatted HTML, for the detail pane. size: points, 0 the default."""
    console = Console(record=True, width=width, file=io.StringIO(), color_system="truecolor", legacy_windows=False)
    console.print(renderable)
    points = round((size or gui_theme.FONT_SIZE) * 1.1, 1)  # <pre> ignores the widget size; monospace reads smaller
    code_format = f'<pre style="font-family:{_MONO}; font-size:{points}pt; color:{{foreground}}">{{code}}</pre>'
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
        on_change: Callable[[list[RepoState], list[PackEntry], dict[str, list[ReviewItem]]], None],
        on_progress: Callable[[str, int, int], None],
        notify: Callable[[str, bool], None],
    ) -> None:
        self.cfg = cfg
        self.on_repo, self.on_change, self.on_progress, self.notify = on_repo, on_change, on_progress, notify
        self.states: dict[str, RepoState] = {}
        self.prs = github.PrData()
        self.pack_info: PackInfo | None = None
        self.mine: list[ReviewItem] = []
        self.reviews: list[ReviewItem] = []
        self.team_reviews = False  # also the requests that reach you through a team (slow to search)
        self.org: github.OrgPrs | None = None  # every open PR of the org, searched when All PRs opens
        self.org_searching = False
        self.busy = ""
        self.depth = 0  # steps of the job in progress: a cleanup scans, a clone too
        self.tool: RepoState | None = None  # the tool's own checkout, once fetched at start
        self.tool_updated = False

    @contextlib.asynccontextmanager
    async def job(self):
        """A step of what a UI started. The UI hears that the job is over once, when its outermost step ends:
        a cleanup's switch and scan must not hand the toolbar back halfway."""
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            if not self.depth:
                self.on_progress("", 0, 0)

    def _changed(self) -> None:
        entries = list(self.pack_info.entries) if self.pack_info else []
        org = list(self.org.items) if self.org else []
        prs = {"mine": list(self.mine), "reviews": list(self.reviews), "org": org}  # PR tab -> its PRs
        self.on_change(sorted(self.states.values(), key=ops.by_repo), entries, prs)

    async def load(self, fetch: bool = False) -> None:
        worker = ops.fetch_repo if fetch else ops.scan_repo
        if not await self.batch("Fetching" if fetch else "Scanning", lambda p: worker(self.cfg, p), reset=True):
            return
        # Independent sources: none of them should hold the others up.
        await asyncio.gather(self._load_prs(), self._load_pack(), self._load_mine(), self._load_reviews(quiet=True))

    async def batch(
        self,
        label: str,
        worker: Callable[[Path], Awaitable[RepoState]],
        paths: list[Path] | None = None,
        reset: bool = False,
    ) -> bool:
        async with self.job():
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

    async def _load_mine(self) -> None:
        items, error = await github.fetch_mine()
        if items or not error:
            self.mine = items
            self._changed()

    async def _load_reviews(self, quiet: bool = False) -> None:
        items, error = await github.fetch_reviews(self.team_reviews)
        if error and not quiet:
            self.notify(f"Review requests: {error}", True)
        if items or not error:
            self.reviews = items
            self._changed()

    async def reload_prs(self) -> None:
        """Load your PRs, review requests and branch PRs again, without rescanning the repos."""
        async with self.job():
            await asyncio.gather(self._load_prs(), self._load_mine(), self._load_reviews())

    async def open_org(self) -> None:
        """Show the saved org search at once; search again only when it is missing or old, like the TUI."""
        if self.org_searching:
            return
        if self.org is None:
            self.org = github.cached_org(self.cfg.org)
            if self.org:
                self._changed()
        if self.org is None or time.time() - self.org.fetched > github.ORG_MAX_AGE:
            await self.reload_org()

    async def reload_org(self) -> None:
        if self.org_searching:
            self.notify(f"Still searching the PRs of {self.cfg.org}", False)
            return
        self.org_searching = True
        self.notify(f"Searching all open PRs of {self.cfg.org}, this takes a while...", False)
        try:
            found = await github.fetch_org(self.cfg.org)
        finally:
            self.org_searching = False
        if found.error:
            self.notify(f"Org PRs: {found.error}", True)
        if found.items or not found.error:
            self.org = found
            self._changed()
            if not found.error:
                self.notify(f"Searched all open PRs of {self.cfg.org}", False)

    async def list_org_jobs(self, then: Callable[[list[CloneJob]], None]) -> None:
        """Clone jobs for the org repos not checked out here (archived ones left out), handed to `then`."""
        org = self.cfg.org
        self.notify(f"Listing the repos of {org}...", False)
        repos, error = await github.org_repos(org)
        if error:
            self.notify(f"{org} repos: {error}", True)
            if not repos:
                return
        repos = [r for r in repos if not r.archived]
        then(ops.org_jobs(repos, self.states.values(), self.cfg.root, self.cfg.clone_protocol))

    async def clone_many(self, jobs: list[CloneJob]) -> None:
        """Clone a few at a time, then scan what was cloned so it shows up with the other checkouts."""
        async with self.job():
            done = 0

            def on_done(job: CloneJob, why: str | None) -> None:
                nonlocal done
                done += 1
                self.on_progress("Cloning", done, len(jobs))

            failed = await ops.clone_all(jobs, min(4, self.cfg.concurrency), on_done)
            failed_jobs = {job for job, _ in failed}
            cloned = [j.dest for j in jobs if j.dest.is_dir() and j not in failed_jobs]
            if cloned:
                await self.batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), cloned)
            for job, why in failed[:FAILURES_SHOWN]:
                self.notify(f"{job.name}: {why}", True)
            self.notify(f"Cloned {len(cloned)} repos" + (f", {len(failed)} failed" if failed else ""), bool(failed))

    async def download_jars(self) -> None:
        """The jars of closed-source pack mods, into the folder DreamAssemblerXXL expects."""
        async with self.job():
            entries = self.pack_info.entries if self.pack_info else []
            folder = self.cfg.root / pack.JAR_FOLDER
            report = await pack.download_closed(entries, folder)
            for line in report.failed:
                self.notify(line, True)
            self.notify(f"{report.summary} in {folder}", bool(report.failed))

    async def delete_jars(self, files: list[Path]) -> None:
        async with self.job():
            freed, failed = await asyncio.to_thread(ops.delete_files, files)
            for line in failed[:FAILURES_SHOWN]:
                self.notify(line, True)
            more = f", {len(failed)} could not be deleted (in use?)" if failed else ""
            self.notify(f"Freed {render.megabytes(freed)}{more}", bool(failed))

    async def install_skill(self, agents: list[str]) -> None:
        async with self.job():
            done = []
            for agent in agents:
                try:
                    folder, _ = await asyncio.to_thread(skill.install, None, agent)
                except OSError as e:
                    self.notify(f"{skill.AGENTS[agent]}: {e}", True)
                    return
                done.append(f"{skill.AGENTS[agent]} in {folder}")
            self.notify(f"Skill installed for {', '.join(done)}; new sessions pick it up", False)

    async def toggle_team(self, on: bool) -> None:
        """Show or hide the review requests that reach you through a team, as t does in the terminal UI."""
        async with self.job():
            self.team_reviews = on
            await self._load_reviews()

    async def checkout(self, item: ReviewItem, st: RepoState) -> None:
        """Check out a pull request in its local repo, as c does on the terminal UI's PR tabs."""
        async with self.job():
            head, error = await github.pr_head(item.repo, item.pr.number, self.cfg.clone_protocol)
            if not head:
                self.notify(f"#{item.pr.number}: {error}", True)
                return
            if not await self.batch("Checking out", lambda p: ops.checkout_pr(self.cfg, st, head), [st.path]):
                return
            new = self.states.get(str(st.path))
            if new and new.note:
                if new.note.kind != "failed" and new.head:
                    self.prs.prs[(new.name, new.head)] = item.pr  # link the PR now, not at the next PR reload
                    github.attach([new], self.prs)
                    self._changed()
                self.notify(f"{st.name}: {new.note.text}", new.note.kind == "failed")

    async def find_cleanup(self, states: list[RepoState], then: Callable[[list, list], None]) -> None:
        """Merged branches safe to delete, and kept ones, handed to `then`, as the cleanup asks before deleting."""
        async with self.job():
            safe, unpushed = await ops.cleanup_candidates(states, parked=True)
        then(safe, unpushed)

    async def cleanup(self, safe: list, parked: dict[Path, RepoState], switch: bool) -> None:
        """Delete merged branches; with switch, the parked repos go back to main first so theirs can go too."""
        async with self.job():
            if parked and switch:
                await self.batch("Switching", lambda p: ops.to_main(self.cfg, parked[p]), list(parked))
            # Whatever is still checked out (unticked, or the switch failed) stays.
            safe = [(st, br) for st, br in safe if self.states.get(str(st.path), st).head != br.name]
            if not safe:
                self.notify("Nothing deleted: the merged branches are still checked out", False)
                return
            report = await ops.cleanup(safe)
            note, failed = tui.finish_cleanup(report)
            await self.batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), list({st.path for st, _ in safe}))
            self.notify(note, failed)

    async def to_main_many(self, candidates: dict[Path, RepoState]) -> None:
        if await self.batch("Switching", lambda p: ops.to_main(self.cfg, candidates[p]), list(candidates)):
            self.notify("Switched. Cleanup deletes the merged branches.", False)

    async def check_tool(self) -> None:
        """Fetch the tool's own checkout once per start, and say when it has new commits."""
        path = tui.tool_checkout()
        if path is None:
            return
        self.tool = await ops.fetch_repo(self.cfg, path)
        behind = tui.tool_behind(self.tool)
        if behind:
            self.notify(f"NH Workbench has {behind} new commit(s): Settings (⚙) updates it", False)

    async def update_tool(self) -> None:
        async with self.job():
            self.tool, message, updated = await tui.update_tool(self.cfg, self.tool)
            self.tool_updated = self.tool_updated or updated
            if message:
                self.notify(message, True)

    async def _load_pack(self, force: bool = False) -> None:
        try:
            self.pack_info = await pack.load(self.cfg, force)
        except pack.PackError as e:
            self.notify(str(e), True)
        await self._relink()
        self._changed()

    async def refresh_pack(self) -> None:
        """Download the pack data again instead of using the copy cached for an hour."""
        async with self.job():
            await self._load_pack(force=True)

    async def clone(self, entry: PackEntry) -> None:
        """Clone a pack mod into the root, then scan it so it shows up with the other checkouts."""
        async with self.job():
            res = await pack.clone(entry, self.cfg.root, self.cfg.clone_protocol)
            if not res.ok:
                self.notify(f"{entry.name}: {res.message}", True)
                return
            job = pack.clone_job(entry, self.cfg.root, self.cfg.clone_protocol)
            await self.batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), [job.dest])
            self.notify(f"Cloned {entry.repo} into {job.dest}", False)

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
        self.stopping = False

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        """Cancel what is still running (a fetch when the window closes), let it clean up, then stop."""
        if self.stopping:  # the window was closed before
            return
        self.stopping = True

        async def shutdown() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.loop.stop()

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        self.thread.join(timeout=3)  # git and gh processes killed before the app exits, not left behind


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


def _runs(rows: list[int]) -> list[tuple[int, int]]:
    """Sorted row numbers as (first, last) runs of consecutive ones."""
    runs: list[tuple[int, int]] = []
    for row in rows:
        if runs and runs[-1][1] == row - 1:
            runs[-1] = (runs[-1][0], row)
        else:
            runs.append((row, row))
    return runs


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
        """Update in place rather than reset, so the views keep their selection and scroll position.

        The order of the rows here does not matter: the views sort them.
        """
        new = {self.key(obj): obj for obj in rows}
        gone = [i for i, obj in enumerate(self.rows) if self.key(obj) not in new]
        for first, last in reversed(_runs(gone)):
            self.beginRemoveRows(QModelIndex(), first, last)
            del self.rows[first : last + 1], self._cells[first : last + 1]
            self.endRemoveRows()
        at = {self.key(obj): i for i, obj in enumerate(self.rows)}
        added = []
        for key, obj in new.items():
            if key in at:
                self.rows[at[key]], self._cells[at[key]] = obj, self._render(obj)
            else:
                added.append(obj)
        if self.rows:  # one signal for all: the views sort and filter the rows again
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))
        if added:
            self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(added) - 1)
            self.rows += added
            self._cells += [self._render(obj) for obj in added]
            self.endInsertRows()

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
        cells = repo_cells(st, render.NO_CLIP)
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
        return render.pack_row(entry, render.NO_CLIP)

    def key(self, entry: PackEntry) -> str:
        return entry.name


class PrModel(RichModel):
    """Pull requests in the order GitHub gives them (newest update first), sortable by size like the TUI."""

    def __init__(self, theme: TerminalTheme) -> None:
        super().__init__(theme)
        self.states: list[RepoState] = []  # the checkouts, for what a PR has here
        self.order: dict[str, int] = {}

    def set_rows(self, rows: list) -> None:
        self.order = {item.pr.url: i for i, item in enumerate(rows)}
        super().set_rows(rows)

    def key(self, item: ReviewItem) -> str:
        return item.pr.url

    def sort_key(self, item: ReviewItem, column: str, plain: str) -> object:
        if column == "Updated":
            return self.order.get(item.pr.url, 0)
        if column == "Size":
            return item.pr.size
        return plain.lower()


class MineModel(PrModel):
    """Your open PRs, with the local branch each one comes from."""

    columns = render.MINE_COLUMNS

    def cells(self, item: ReviewItem) -> list[Text]:
        local = local_branch(item, self.states)
        return render.mine_row(item, local[1] if local else "", render.NO_CLIP)


class ReviewModel(PrModel):
    """Open PRs waiting on your review, yours personally or through a team."""

    columns = render.REVIEW_COLUMNS

    def cells(self, item: ReviewItem) -> list[Text]:
        return render.review_row(item, render.NO_CLIP)


class ToolModel(RichModel):
    """The Tools tab: one line per tool, as in the terminal UI."""

    columns = TOOL_COLUMNS

    def cells(self, tool: tuple) -> list[Text]:
        return [Text(tool[1], "bold"), Text(tool[2])]

    def key(self, tool: tuple) -> str:
        return tool[0]


class OrgModel(PrModel):
    """Every open PR of the org, marked when you wrote it, are asked to review it, or reviewed it."""

    columns = render.ORG_COLUMNS

    def __init__(self, theme: TerminalTheme) -> None:
        super().__init__(theme)
        self.mark: Callable[[ReviewItem], str] = lambda item: ""

    def cells(self, item: ReviewItem) -> list[Text]:
        return render.org_row(item, self.mark(item), render.NO_CLIP)


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
    """A sortable, filterable table of a RichModel. Keys go to `on_key`, for the TUI's keys."""

    def __init__(
        self, model: RichModel, accept: Callable[[object], bool], row_height: int, sort_column: str = ""
    ) -> None:
        super().__init__()
        self.source = model
        self.dragged = False  # a column was sized by hand: the contents no longer size them
        self._fitting = False
        self.proxy = RichFilter(accept)
        self.proxy.setSourceModel(model)
        self.on_key: Callable[[str], bool] = lambda key: False  # a printable key, or tab, backtab, escape
        self.setModel(self.proxy)
        self.setItemDelegate(RichDelegate(self))
        self.setSortingEnabled(True)
        column = model.columns.index(sort_column) if sort_column in model.columns else 0
        self.sortByColumn(column, Qt.SortOrder.AscendingOrder)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(row_height)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setResizeContentsPrecision(-1)  # every row: a long name far down must fit too
        self.horizontalHeader().sectionResized.connect(self._resized)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def fit_columns(self) -> None:
        """Size the columns to their contents, the last one still taking the width that is left."""
        self._fitting = True
        try:
            self.resizeColumnsToContents()
            header = self.horizontalHeader()
            header.setStretchLastSection(False)  # sizing the last section turns its stretch off until set again
            header.setStretchLastSection(True)
        finally:
            self._fitting = False

    def _resized(self, section: int, old: int, new: int) -> None:
        # The last column follows the window's width; any other one changing outside fit_columns was dragged.
        if not self._fitting and section != len(self.source.columns) - 1:
            self.dragged = True

    def current(self) -> object | None:
        index = self.currentIndex()
        return index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None

    def keyPressEvent(self, event) -> None:
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if mods in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
            named = {Qt.Key.Key_Tab: "tab", Qt.Key.Key_Backtab: "backtab", Qt.Key.Key_Escape: "escape"}
            text = event.text()
            key = named.get(event.key()) or (text if text.isprintable() and text.strip() else "")
            if key and self.on_key(key):
                return
        super().keyPressEvent(event)


class FilterBox(QLineEdit):
    """The name filter: Esc clears it, Enter or Down goes back to the list, as in the terminal UI."""

    def __init__(self, leave: Callable[[], None]) -> None:
        super().__init__()
        self.leave = leave

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.clear()
            self.leave()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Down):
            self.leave()
        else:
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


def pr_actions(item: ReviewItem, st: RepoState | None, cfg: Config) -> list[tuple[str, str, str, str | None]]:
    """(key, id, label, reason it is unavailable) for a pull request, with the TUI's PR tab keys."""
    here = None if st else f"{item.repo} is not checked out here"
    no_checkout = here or (f"{st.name} has uncommitted changes" if st and st.dirty else None)
    return [
        ("w", "web", f"Open #{item.pr.number} in the browser", None),
        ("c", "checkout", f"Check out #{item.pr.number}" + (f" in {st.name}" if st else ""), no_checkout),
        ("o", "ide", "Open in IntelliJ", here or ("turned off in Settings" if cfg.ide_off else None)),
        ("e", "folder", "Open folder", here),
    ]


class HistoryPopup(QListWidget):
    """The status bar's earlier messages, newest first. Closes on Esc or a click elsewhere; Ctrl+C copies."""

    def __init__(self, parent: QWidget, messages: list[tuple[str, str, bool]], warning: str) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setObjectName("history")  # framed, by the style sheet
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for when, text, warned in reversed(messages):
            item = QListWidgetItem(f"{when}  {text}")
            if warned:
                item.setForeground(QColor(warning))
            self.addItem(item)
        if not messages:
            item = QListWidgetItem("No messages yet")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addItem(item)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.matches(QKeySequence.StandardKey.Copy):
            QApplication.clipboard().setText("\n".join(item.text() for item in self.selectedItems()))
        else:
            super().keyPressEvent(event)


class ConfirmDialog(QDialog):
    """Yes or no on a list (HTML), with an optional checkbox. No is the default: Enter alone never confirms."""

    def __init__(self, parent: QWidget, title: str, body: str, option: str = "", checked: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("NH Workbench")
        self.resize(820, 440)
        head = QLabel(title)
        head.setObjectName("title")  # bigger and bold, by the style sheet
        text = QTextBrowser()
        text.setHtml(body)
        self.option = QCheckBox(option)
        self.option.setChecked(checked)
        self.option.setVisible(bool(option))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Yes).setAutoDefault(False)
        no = buttons.button(QDialogButtonBox.StandardButton.No)
        no.setDefault(True)
        no.setFocus()
        layout = QVBoxLayout(self)
        layout.addWidget(head)
        layout.addWidget(text, 1)
        layout.addWidget(self.option)
        layout.addWidget(buttons)

    def checked(self) -> bool:
        return self.option.isChecked()


class KeepDialog(QDialog):
    """Tick the branches of a repo that the cleanup must never delete."""

    def __init__(self, parent: QWidget, st: RepoState, kept: tuple[str, ...]) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Protect branches of {st.name}")
        self.resize(460, 380)
        self.list = QListWidget()
        local = [b.name for b in st.branches]
        for name in local + [k for k in kept if k not in local]:
            item = QListWidgetItem(name if name in local else f"{name}  (no local branch)")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in kept else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Ticked branches are never deleted by the cleanup."))
        layout.addWidget(self.list, 1)
        layout.addWidget(buttons)

    def chosen(self) -> list[str]:
        items = [self.list.item(i) for i in range(self.list.count())]
        return [i.data(Qt.ItemDataRole.UserRole) for i in items if i.checkState() == Qt.CheckState.Checked]


# --- the window -----------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, root_override: Path | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.root_override = root_override  # --root: wins over the config file until the folder is changed
        self.bridge = Bridge()
        self.loop = AsyncLoop()
        self.theme_name = gui_theme.theme_name(cfg.theme)
        self.theme = theme_for(QApplication.palette(), self.theme_name)
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
        self.run(self.wb.check_tool())
        self.refresh_timer = QTimer(self)  # checked every minute, so a new interval applies without a restart
        self.refresh_timer.timeout.connect(self._auto_refresh)
        self.refresh_timer.start(60_000)

    def _build(self) -> None:
        self.running = ""  # label of the batch in progress
        self.progress_text = ""  # what the progress put in the status bar, replaced when it ends
        self.last_loaded = 0.0  # time.monotonic() of the last rescan, for auto-refresh
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
                "Fetch every repo (f; F: the selected one)",
            ),
            (
                "pull",
                "Pull safe",
                "Ctrl+Shift+P",
                lambda: self._operate("Pulling", ops.pull_repo),
                "Fast-forward main if safe (p; P: the selected one)",
            ),
            (
                "to_main",
                "To main",
                "Ctrl+Shift+M",
                lambda: self._to_main_all(),
                "Switch repos parked on a merged PR branch back to main (m)",
            ),
            (
                "cleanup",
                "Cleanup",
                "Ctrl+Shift+Del",
                lambda: self._cleanup(list(self.model.rows)),
                "Delete merged local branches (d)",
            ),
            ("rescan", "Rescan", "F5", lambda: self._rescan(), "Read every repo again (r)"),
            ("reload_prs", "Refresh from GitHub", "F5", self._reload_prs, "Load your PRs and reviews again (r)"),
            ("reload_org", "Search again", "F5", lambda: self.run(self.wb.reload_org()), "Search the org's PRs (r)"),
            ("clone", "Clone", "Ctrl+Shift+C", lambda: self._act("clone"), "Clone the selected mod (c)"),
            ("checkout", "Check out", "Ctrl+Shift+K", lambda: self._act("checkout"), "Check out the selected PR (c)"),
            ("toggle_team", "Team reviews", "Ctrl+Shift+T", self._toggle_team, "Also requests made to your teams (t)"),
            ("toggle_drafts", "Drafts", "Ctrl+Shift+D", self._refresh_org, "Also list draft PRs (t)"),
            ("pr_filters", "Show only...", "Ctrl+Shift+V", self._pr_filters, "Only some of the org's PRs (v)"),
            (
                "open_web",
                "Open in browser",
                "Ctrl+Shift+G",
                lambda: self._act("web"),
                "PR or GitHub page of the line (w)",
            ),
            (
                "open_ide",
                "Open in IntelliJ",
                "Ctrl+Shift+O",
                lambda: self._act("ide"),
                "Open the checkout of the line (o)",
            ),
        ):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(keys))
            action.setToolTip(f"{tip} ({keys})")
            action.triggered.connect(slot)
            bar.addAction(action)
            self.tab_actions[action_id] = action
        self.toolbar = bar
        for key in ("toggle_team", "toggle_drafts"):
            self.tab_actions[key].setCheckable(True)
        batch = ("fetch", "pull", "to_main", "cleanup", "rescan", "reload_prs", "clone", "checkout", "toggle_team")
        self.batch_actions = [self.tab_actions[k] for k in batch]
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.filter = FilterBox(lambda: self.table.setFocus())
        self.filter.setPlaceholderText("Filter by name (Ctrl+F)")
        self.filter.setClearButtonEnabled(True)
        self.filter.setFixedWidth(260)
        self.filter.textChanged.connect(lambda _: self._apply_view())
        self.filter_action = bar.addWidget(self.filter)
        find = QAction(self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: (self.filter.setFocus(), self.filter.selectAll()))
        self.addAction(find)
        cog = QToolButton()
        cog.setText("⚙")
        cog.setObjectName("cog")  # bigger, by the style sheet
        cog.setAutoRaise(True)
        cog.setToolTip("Settings (x, Ctrl+,)")
        cog.clicked.connect(lambda: self.open_settings())
        bar.addWidget(cog)
        for keys, slot in (
            ("Ctrl+,", lambda: self.open_settings()),
            ("Ctrl+Tab", lambda: self._cycle_view(1)),
            ("Ctrl+Shift+Tab", lambda: self._cycle_view(-1)),
            ("Ctrl+Q", self.close),
        ):
            shortcut = QAction(self)
            shortcut.setShortcut(QKeySequence(keys))
            shortcut.triggered.connect(slot)
            self.addAction(shortcut)

        self.tabs = QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        for key in VIEW_KEYS:
            if key in TAB_GROUP_STARTS:  # a divider between the repo, PR and other tabs: a tab that cannot be picked
                self.tabs.setTabEnabled(self.tabs.addTab("│"), False)
            self.tabs.setTabData(self.tabs.addTab(VIEWS[key][0]), key)
        self.tabs.currentChanged.connect(lambda _: self._apply_view())

        height = gui_theme.row_height(self.cfg.font_size)
        self.repos = RichTable(RepoModel(self.theme), self._repo_shown, height)
        self.mine = RichTable(MineModel(self.theme), self._pr_shown, height, sort_column="Updated")
        self.reviews = RichTable(ReviewModel(self.theme), self._pr_shown, height, sort_column="Updated")
        self.org_urls: set[str] = set()  # the org PRs that pass the Drafts and Show only choices
        self.org = RichTable(OrgModel(self.theme), self._org_pr_shown, height, sort_column="Updated")
        self.org.source.mark = self._org_mark
        self.pack = RichTable(PackModel(self.theme), self._entry_shown, height)
        # Tabs with their own table; the repo tabs share one.
        self.tools = RichTable(ToolModel(self.theme), lambda tool: True, height)
        self.tools.source.set_rows(TOOLS)
        self.tools.setSortingEnabled(False)
        self.tools.proxy.sort(-1)  # back to the terminal UI's order
        self.tools.fit_columns()
        self.spark_window = None
        self.tables = {"mine": self.mine, "reviews": self.reviews, "org": self.org, "pack": self.pack}
        self.tables["tools"] = self.tools
        self.stack = QStackedWidget()
        for table in (self.repos, *self.tables.values()):
            table.customContextMenuRequested.connect(lambda pos, t=table: self._menu(t.viewport().mapToGlobal(pos)))
            table.selectionModel().currentRowChanged.connect(lambda *_: self._update_detail())
            self.stack.addWidget(table)
        for table in (self.repos, *self.tables.values()):
            table.on_key = self._key
        self.repos.activated.connect(lambda _: self._menu())  # Enter or double click
        self.pack.activated.connect(lambda _: self._menu())
        self.tools.activated.connect(lambda _: self._open_tool(self._selected_tool()))  # as in the TUI: Enter runs it
        for key in PR_TABS:  # as in the terminal UI: Enter opens the PR
            self.tables[key].activated.connect(lambda _: self._act("web"))

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
        self.history: list[tuple[str, str, bool]] = []  # (time, text, warning), oldest first
        self.unseen_warnings = 0
        self.history_popup: HistoryPopup | None = None
        self.history_button = QToolButton()
        self.history_button.setAutoRaise(True)
        self.history_button.setToolTip("Earlier messages")
        self.history_button.clicked.connect(self._show_history)
        self.statusBar().addPermanentWidget(self.history_button)
        self._update_history_button()
        self._show_actions()

    @property
    def model(self) -> RepoModel:
        return self.repos.source

    @property
    def table(self) -> RichTable:
        return self.tables.get(self._view(), self.repos)

    # --- plumbing between the two threads

    def post(self, fn: Callable[[], None]) -> None:
        """Run fn on the Qt thread, from any thread; dropped once the window is gone."""
        try:
            self.bridge.call.emit(fn)
        except RuntimeError:  # the window was closed while the work ran: nothing left to show it in
            pass

    def _later(self, fn: Callable) -> Callable:
        """Wrap fn so that calling it from the asyncio thread runs it on the Qt thread."""
        return lambda *args: self.post(lambda: fn(*args))

    def run(self, coro: Coroutine) -> None:
        future = self.loop.submit(coro)

        def done(f: concurrent.futures.Future) -> None:
            if not f.cancelled() and f.exception():
                log.error("background task failed", exc_info=f.exception())
                self.post(lambda: self._notify(f"Failed: {f.exception()}", True))

        future.add_done_callback(done)

    def closeEvent(self, event) -> None:
        if self.spark_window is not None:  # a window of its own: it would outlive the loop it runs on
            self.spark_window.close()
        self.loop.stop()
        super().closeEvent(event)

    # --- results coming in

    def _repo_loaded(self, st: RepoState) -> None:
        self.model.update_row(st)
        self._count_tabs()
        if self._selected() and self._selected().path == st.path:
            self._update_detail()

    def _all_loaded(self, states: list[RepoState], entries: list[PackEntry], prs: dict[str, list[ReviewItem]]) -> None:
        loaded = [(self.repos, states), (self.pack, entries)]
        for key in PR_TABS:
            self.tables[key].source.states = states
            loaded.append((self.tables[key], prs.get(key, [])))
        for table, rows in loaded:
            table.source.set_rows(rows)  # in place: the selected line stays selected unless it is gone
            if not table.currentIndex().isValid() and table.proxy.rowCount():
                table.selectRow(0)
        self.pack.source.set_headers(render.pack_labels(self.wb.pack_info))
        self._refresh_org()
        for table, _ in loaded:
            # Sized to what they hold now (pack versions come after the repos), until a column is dragged.
            if not table.dragged and table.proxy.rowCount():
                table.fit_columns()

    def _progress(self, label: str, done: int, total: int) -> None:
        if not label:
            self._busy(False)
            self.model.unmark()
            self.progress.hide()
            if self.progress_text and self.message.text() == self.progress_text:  # not a message shown since
                self.message.setText(f"{len(self.model.rows)} repos")
            self.progress_text = ""
            if self._view() == "tools":  # what a tool would act on may have changed
                self._update_detail()
            return
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.progress.show()
        self.progress_text = f"{label}..."
        self._show_message(self.progress_text)  # passing state, not kept in the history

    def warning_color(self) -> str:
        return gui_theme.warning_color(self.theme_name)

    def set_theme(self, name: str) -> None:
        """Switch the colors of the whole app, the Rich text in the tables and the detail pane included."""
        gui_theme.apply(QApplication.instance(), name, self.cfg.font_size)
        self.theme_name = name
        self.theme = theme_for(QApplication.palette(), name)
        for table in (self.repos, *self.tables.values()):
            table.source.theme = self.theme
            table.source.set_rows(table.source.rows)  # rendered again, in place
        self._update_detail()

    def set_font_size(self, size: int) -> None:
        """Change the text size everywhere, then size the rows and columns to it."""
        gui_theme.apply(QApplication.instance(), self.theme_name, size)
        height = gui_theme.row_height(size)
        for table in (self.repos, *self.tables.values()):
            table.verticalHeader().setDefaultSectionSize(height)
            if not table.dragged:
                table.fit_columns()
        self._update_detail()

    def _notify(self, text: str, warning: bool = False) -> None:
        """Show a message in the status bar and keep it in the history."""
        self._show_message(text, warning)
        self.history = [*self.history[-(HISTORY_SIZE - 1) :], (datetime.now().strftime("%H:%M:%S"), text, warning)]
        if warning:
            self.unseen_warnings += 1
        self._update_history_button()

    def _show_message(self, text: str, warning: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {self.warning_color()};" if warning else "")

    def _update_history_button(self) -> None:
        n = self.unseen_warnings
        self.history_button.setText(f"History  ⚠ {n}" if n else "History")

    def _show_history(self) -> None:
        self.unseen_warnings = 0
        self._update_history_button()
        popup = HistoryPopup(self, self.history, self.warning_color())
        width, height = min(820, self.width() - 40), 340
        button = self.history_button
        popup.resize(width, height)
        popup.move(button.mapToGlobal(QPoint(button.width() - width, -height)))
        popup.show()
        popup.setFocus()
        self.history_popup = popup  # kept, or it is collected at once

    # --- view

    def _view(self) -> str:
        return self.tabs.tabData(self.tabs.currentIndex()) or "all"

    def tab_index(self, key: str) -> int:
        return next(i for i in range(self.tabs.count()) if self.tabs.tabData(i) == key)

    def select_view(self, key: str) -> None:
        self.tabs.setCurrentIndex(self.tab_index(key))

    def _needle(self) -> str:
        return self.filter.text().lower()

    def _repo_shown(self, st: RepoState) -> bool:
        keep = VIEWS.get(self._view(), ("", None))[1]
        return bool(keep and keep(st)) and self._needle() in st.name.lower()

    def _entry_shown(self, entry: PackEntry) -> bool:
        return self._needle() in entry.name.lower()

    def _pr_shown(self, item: ReviewItem) -> bool:
        return pr_matches(item, self._needle())

    def _org_pr_shown(self, item: ReviewItem) -> bool:
        return item.pr.url in self.org_urls and pr_matches(item, self._needle())

    def _org_mark(self, item: ReviewItem) -> str:
        reviewed = self.wb.org.reviewed if self.wb.org else set()
        return org_mark(item, self.mine.source.rows, self.reviews.source.rows, reviewed)

    def _refresh_org(self) -> None:
        """Apply the Drafts and Show only choices to the org PRs, by the terminal UI's rule."""
        drafts = self.tab_actions["toggle_drafts"].isChecked()
        states, entries = self.model.rows, self.pack.source.rows
        shown = org_shown(self.org.source.rows, drafts, self.cfg.pr_filters, states, entries)
        self.org_urls = {i.pr.url for i in shown}
        self.org.proxy.refilter()
        if self.org.current() is None and self.org.proxy.rowCount():
            self.org.selectRow(0)
        self._count_tabs()
        self._update_detail()

    def _show_actions(self) -> None:
        """Keep the toolbar to what works on the current tab, by the terminal UI's own rule."""
        view = self._view()
        item = self._selected_pr()
        # Only asked on PR tabs: there, no selected PR means no checkout to open (as in the TUI).
        has_repo = item is not None and repo_for(item, self.model.rows) is not None
        for action_id, action in self.tab_actions.items():
            action.setVisible(action_shown(action_id, view, self.cfg.ide_off, has_repo))
        self.filter_action.setVisible(action_shown("filter", view))

    def _apply_view(self) -> None:
        if self._view() == "org":
            self.run(self.wb.open_org())
        self._show_actions()
        table = self.table
        self.stack.setCurrentWidget(table)
        table.proxy.refilter()
        if table.current() is None and table.proxy.rowCount():
            table.selectRow(0)
        self._update_detail()

    def _count_tabs(self) -> None:
        for key in VIEW_KEYS:
            label, keep = VIEWS[key]
            if key == "reviews":  # as in the terminal UI: only the requests made to you personally
                n = sum(1 for item in self.reviews.source.rows if item.direct)
            elif key == "org":
                n = len(self.org_urls) if self.wb.org else None  # not searched yet
            elif key == "tools":
                n = None
            elif key in self.tables:
                n = len(self.tables[key].source.rows)
            else:
                n = sum(1 for st in self.model.rows if keep and keep(st))
            self.tabs.setTabText(self.tab_index(key), label if n is None else f"{label}  {n}")

    def _selected(self) -> RepoState | None:
        return self.repos.current() if self._view() in REPO_VIEWS else None

    def _selected_pr(self) -> ReviewItem | None:
        return self.tables[self._view()].current() if self._view() in PR_TABS else None

    def _selected_entry(self) -> PackEntry | None:
        return self.pack.current() if self._view() == "pack" else None

    def _selected_tool(self) -> str | None:
        tool = self.tools.current() if self._view() == "tools" else None
        return tool[0] if tool else None

    def _update_detail(self) -> None:
        self._show_actions()  # Open in IntelliJ on a PR depends on its repo being here
        st, entry, item = self._selected(), self._selected_entry(), self._selected_pr()
        if item and self._view() == "mine":
            content = mine_detail(item, local_branch(item, self.model.rows))
        elif item and self._view() == "org":
            content = org_detail(item, self._org_mark(item), repo_for(item, self.model.rows))
        elif item:
            content = review_detail(item, repo_for(item, self.model.rows))
        elif st:
            content = repo_detail(st, self.wb.pack_info)
        elif entry and self.wb.pack_info:
            content = pack_detail(entry, self.wb.pack_info)
        elif self._selected_tool():
            tool = self.tools.current()
            status = tool_status(tool[0], self.cfg, self.pack.source.rows, self.model.rows)
            content = Group(Text(tool[3]), Text(status, "bold"))
        else:
            self.detail.clear()
            return
        width = max(60, self.detail.viewport().width() // max(1, self.detail.fontMetrics().horizontalAdvance("M")))
        self.detail.setHtml(rich_html(content, self.theme, width, self.cfg.font_size))

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

    def _rescan(self, fetch: bool = False) -> None:
        if self._start("Fetching" if fetch else "Scanning", None) is not None:
            self.last_loaded = time.monotonic()
            self.run(self.wb.load(fetch))

    def _auto_refresh(self) -> None:
        """Rescan like Rescan does once the interval has passed, unless something else is going on."""
        minutes = self.cfg.refresh_minutes
        if not minutes or self.running or QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return
        if time.monotonic() - self.last_loaded >= minutes * 60:
            self._rescan(fetch=self.cfg.refresh_fetch)

    # --- settings

    def open_settings(self, focus: str = "") -> None:
        from .gui_settings import SettingsDialog

        SettingsDialog(self, focus).exec()

    def apply_settings(self, changes: list) -> bool:
        """Write the Settings dialog's changes to the config file and reload it. False when that failed."""
        if not changes:
            return True
        new_root = any(keys == ("root",) for keys, _ in changes)
        try:
            for keys, value in changes:
                path = config.save_setting(keys, value)
            cfg = config.load(None if new_root else self.root_override)
        except config.ConfigError as e:
            QMessageBox.warning(self, "Settings", str(e))
            return False
        if new_root:
            self.root_override = None
        self.cfg = self.wb.cfg = cfg
        self.setWindowTitle(f"NH Workbench - {cfg.root}")
        self._show_actions()
        if gui_theme.theme_name(cfg.theme) != self.theme_name:
            self.set_theme(gui_theme.theme_name(cfg.theme))
        if any(keys == ("font_size",) for keys, _ in changes):
            self.set_font_size(cfg.font_size)
        self._notify(f"Saved to {path}")
        if any(keys[0] in ("root", "repos") for keys, _ in changes):  # other repos, or other settings for them
            self._rescan()
        return True

    def refresh_pack(self) -> None:
        if self._start("Downloading the pack data", []) is not None:
            self.run(self.wb.refresh_pack())

    def update_tool(self) -> None:
        if self._start("Updating NH Workbench", []) is not None:
            self.run(self.wb.update_tool())

    def _line(self) -> tuple[str, list[tuple[str, str, str, str | None]], Callable[[str], None]] | None:
        """The selected line's name, its actions, and what runs one: the menu, toolbar and letters all use it."""
        st, entry, item, tool = self._selected(), self._selected_entry(), self._selected_pr(), self._selected_tool()
        if tool:
            name = self.tools.current()[1]
            return name, [("o", "open", f"Open {name}", None)], lambda a: self._open_tool(tool)
        if item:
            local = repo_for(item, self.model.rows)
            title = f"{item.repo} #{item.pr.number}"
            return title, pr_actions(item, local, self.cfg), lambda a: self._pr_action(a, item, local)
        if entry:
            return entry.name, pack_actions(entry, self.cfg), lambda a: self._pack_action(a, entry)
        if st and not st.error:
            return st.name, repo_actions(st, self.cfg.ide_off), lambda a: self._repo_action(a, st)
        return None

    def _menu(self, where=None) -> None:
        """The menu of the selected line: Enter, double click or right click."""
        line = self._line()
        if line:
            title, actions, run = line
            chosen = self._ask(title, actions, where)
            if chosen:
                run(chosen)

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

    def _key(self, key: str) -> bool:
        """The terminal UI's keys, on every tab. True when the key was taken."""
        view = self._view()
        if key in ("tab", "backtab"):
            self._cycle_view(-1 if key == "backtab" else 1)
            return True
        if key == "escape":
            if not self.filter.text():
                return False
            self.filter.clear()
            return True
        anywhere = {
            "/": ("filter", lambda: (self.filter.setFocus(), self.filter.selectAll())),
            "s": ("sort", self._cycle_sort),
            "x": ("", self.open_settings),
            "C": ("", lambda: open_folder("config")),
            "L": ("", lambda: open_folder("logs")),
            "q": ("", self.close),
        }
        if key in anywhere:
            shown_as, run = anywhere[key]
            if not shown_as or action_shown(shown_as, view):
                run()
            return True
        if key in ("F", "P") and view in REPO_VIEWS:  # the selected repo only
            st = self._selected()
            if st:
                self._repo_action("fetch" if key == "F" else "pull", st)
            return True
        return self._letter(key)

    def _cycle_view(self, step: int) -> None:
        keys = VIEW_KEYS
        self.select_view(keys[(keys.index(self._view()) + step) % len(keys)])  # over the dividers
        self.table.setFocus()

    def _cycle_sort(self) -> None:
        """s: the terminal UI's sort orders in turn. A click on a header picks one too."""
        if self._view() in PR_TABS:
            cycle = [(*PR_SORT_COLUMNS[key], label) for key, (label, _) in render.PR_SORTS.items()]
        else:
            by_key = {key: column for column, key in HEADER_SORT.items()}
            cycle = [(by_key[key], ASC, label) for key, (label, _) in SORTS.items()]
        table = self.table
        header, columns = table.horizontalHeader(), table.source.columns
        section = header.sortIndicatorSection()
        now = (columns[section], header.sortIndicatorOrder()) if 0 <= section < len(columns) else None
        at = next((i for i, (column, order, _) in enumerate(cycle) if (column, order) == now), -1)
        column, order, label = cycle[(at + 1) % len(cycle)]
        table.sortByColumn(columns.index(column), order)
        self._notify(f"Sorted by {label}")

    def _letter(self, key: str) -> bool:
        """A toolbar action's letter; on the Pack and PR tabs, also the selected line's (c, w, e...) at once."""
        for letter, action_id in LETTERS:
            action = self.tab_actions[action_id]
            if key == letter and action.isVisible():
                if action.isEnabled():
                    action.trigger()
                return True
        if self._view() in REPO_VIEWS:  # as in the terminal UI: the repo menu's other letters only in the menu
            return False
        line = self._line()
        action_id = next((i for k, i, _, _ in line[1] if k == key), None) if line else None
        if action_id:
            self._act(action_id)
        return action_id is not None

    def _confirm(self, title: str, text: str, details: str = "") -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "NH Workbench", title, parent=self)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setInformativeText(text)
        if details:
            box.setDetailedText(details)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _open_tool(self, key: str | None) -> None:
        if key == "spark":
            from .gui_spark import SparkWindow

            if self.spark_window is None:
                self.spark_window = SparkWindow(self)
            self.spark_window.show()
            self.spark_window.raise_()
            self.spark_window.activateWindow()
        elif key == "clone":
            self._tool_clone()
        elif key == "jars":
            if not any(e.closed for e in self.pack.source.rows):
                self._notify("Pack data not loaded yet", True)
            elif self._start("Downloading closed-source jars", []) is not None:
                self.run(self.wb.download_jars())
        elif key == "clean":
            self._tool_clean()
        elif key == "skill":
            self._tool_skill()

    def _tool_clone(self) -> None:
        protocol, org = self.cfg.clone_protocol, self.cfg.org
        pack_jobs = pack.missing_jobs(self.pack.source.rows, self.cfg.root, protocol)
        no_pack = None if pack_jobs else ("pack data not loaded" if not self.pack.source.rows else "none missing")
        actions = [
            ("p", "pack", f"Pack mods not checked out ({len(pack_jobs)})", no_pack),
            ("o", "org", f"{org} repos not checked out, archived ones left out (asks GitHub)", None),
        ]
        choice = self._ask(f"Clone missing repos into {self.cfg.root} over {protocol.upper()}", actions)
        if choice == "pack":
            self._confirm_clone(pack_jobs, "pack mods")
        elif choice == "org":
            self.run(self.wb.list_org_jobs(self._later(lambda jobs: self._confirm_clone(jobs, f"{org} repos"))))

    def _confirm_clone(self, jobs: list[CloneJob], what: str) -> None:
        if not jobs:
            self._notify(f"Every one of the {what} is checked out already")
            return
        text = "Folders that already exist are skipped."
        if self._confirm(f"Clone {len(jobs)} {what}?", text, "\n".join(j.name for j in jobs)):
            if self._start(f"Cloning {len(jobs)} {what}", []) is not None:
                self.run(self.wb.clone_many(jobs))

    def _tool_clean(self) -> None:
        found = ops.built_jars(self.model.rows, self.cfg.org)
        if not found:
            self._notify(f"No built jars in build/libs of {self.cfg.org} repos")
            return
        files = [p for _, jars in found for p in jars]
        total = render.megabytes(sum(ops.file_size(p) for p in files))
        sizes = [(st, jars, render.megabytes(sum(map(ops.file_size, jars)))) for st, jars in found]
        lines = [f"{st.name}  {len(jars)} jars, {size}" for st, jars, size in sizes]
        title = f"Delete {len(files)} built jars ({total}) in {len(found)} repos?"
        text = "Deleted for good, not to the recycle bin. The next build makes them again."
        if self._confirm(title, text, "\n".join(lines)) and self._start("Deleting built jars", []) is not None:
            self.run(self.wb.delete_jars(files))

    def _tool_skill(self) -> None:
        actions = []
        for key, (agent, label) in zip("123", skill.AGENTS.items(), strict=True):
            state = "update" if skill.installed(agent) else "install"
            actions.append((key, agent, f"{label}: {state} in {skill.skills_dir(agent) / skill.SKILL}", None))
        actions.append(("a", "all", "All", None))
        choice = self._ask("Install the spark-profile skill", actions)
        if choice and self._start("Installing the skill", []) is not None:
            self.run(self.wb.install_skill(list(skill.AGENTS) if choice == "all" else [choice]))

    def _cleanup(self, states: list[RepoState]) -> None:
        """Delete merged branches, after saying which: d in the terminal UI (all repos) or its repo menu (one)."""
        if self._start("Looking for merged branches", []) is not None:
            self.run(self.wb.find_cleanup(states, self._later(self._confirm_cleanup)))

    def _confirm_cleanup(self, safe: list, unpushed: list) -> None:
        if not safe:
            self._notify(tui.no_cleanup_note(unpushed))
            return
        parked = {st.path: st for st, br in safe if br.name == st.head}  # checked out: needs a switch to main
        option = f"Switch {len(parked)} repos to main first (unticked: keep their branch)" if parked else ""
        body = rich_html(tui.cleanup_body(safe, parked), self.theme, 120, self.cfg.font_size)
        title = f"Delete {len(safe)} merged local branches?"
        dialog = ConfirmDialog(self, title, body, option, self.cfg.cleanup_to_main)
        if not dialog.exec():
            return
        switch = bool(parked) and dialog.checked()
        if parked and dialog.checked() != self.cfg.cleanup_to_main:
            self.cfg.cleanup_to_main = dialog.checked()
            try:
                config.save_setting(("cleanup_to_main",), dialog.checked())
            except config.ConfigError as e:
                self._notify(str(e), True)
        if self._start("Cleaning up", list({st.path for st, _ in safe})) is not None:
            self.run(self.wb.cleanup(safe, parked, switch))

    def _to_main_all(self) -> None:
        candidates = {st.path: st for st in ops.to_main_candidates(self.model.rows)}
        if not candidates:
            self._notify("No repo is parked on a merged branch with a clean working tree")
            return
        body = rich_html(tui.to_main_body(candidates.values()), self.theme, 120, self.cfg.font_size)
        if not ConfirmDialog(self, f"Switch {len(candidates)} repos back to main?", body).exec():
            return
        if self._start("Switching", list(candidates)) is not None:
            self.run(self.wb.to_main_many(candidates))

    def _repo_setting(self, action_id: str, st: RepoState) -> None:
        """The repo menu entries that change the config file for one repo."""
        repo = self.cfg.repo(st.name)
        if action_id == "keep":
            dialog = KeepDialog(self, st, repo.keep_branches)
            if dialog.exec() and dialog.chosen() != list(repo.keep_branches):
                self.apply_settings([(("repos", st.name, "keep_branches"), dialog.chosen() or None)])
        elif action_id == "main_branch":
            current = repo.tracked_branch or ""
            names = ["", *(b.name for b in st.branches)]
            if current not in names:
                names.append(current)
            label = "Branch to keep up to date (empty: the automatic choice)"
            branch, ok = QInputDialog.getItem(
                self, f"Main branch for {st.name}", label, names, names.index(current), True
            )
            branch = branch.strip()
            if (
                ok
                and branch != current
                and self.apply_settings([(("repos", st.name, "tracked_branch"), branch or None)])
            ):
                if branch and st.branch(branch) is None:
                    self._notify(f"No local branch {branch} yet: pull will say so until it exists", True)
        elif action_id == "group":
            groups = sorted({s.group for s in self.model.rows} | {st.group})
            label = "Group (empty: the automatic one)"
            group, ok = QInputDialog.getItem(self, f"Group for {st.name}", label, groups, groups.index(st.group), True)
            if ok and group.strip() != st.group:
                self.apply_settings([(("repos", st.name, "group"), group.strip() or None)])
        elif action_id == "hide":
            text = f"Adds ignore = true for {st.name} to the config file. Settings can show it again."
            if self._confirm(f"Hide {st.name} from the tool?", text):
                self.apply_settings([(("repos", st.name, "ignore"), True)])

    def _reload_prs(self) -> None:
        if self._start("Refreshing from GitHub", []) is not None:
            self.run(self.wb.reload_prs())

    def _pr_filters(self) -> None:
        """Pick which org PRs to list, as v does in the terminal UI. Saved in the config file."""
        wanted = set(self.cfg.pr_filters)
        menu = KeyMenu(self)
        menu.addSection("All PRs: show only")
        for (key, label), letter in zip(github.PR_FILTERS.items(), "lpus", strict=True):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key in wanted)
            action.setShortcut(QKeySequence(letter))
            action.setData(key)
        everything = menu.addAction("Show all" if wanted else "Show all  (nothing filtered)")
        everything.setShortcut(QKeySequence("n"))
        everything.setEnabled(bool(wanted))
        everything.setData("none")
        button = self.toolbar.widgetForAction(self.tab_actions["pr_filters"])
        chosen = menu.exec(button.mapToGlobal(button.rect().bottomLeft())) or menu.chosen
        if not chosen:
            return
        choice = chosen.data()
        wanted = set() if choice == "none" else wanted ^ {choice}
        if choice == "unreviewed" and self.wb.org and any(i.pr.reviews is None for i in self.wb.org.items):
            self._notify("Review counts come with the next search; until then no PR is left out for it")
        picked = [f for f in github.PR_FILTERS if f in wanted]
        try:
            config.save_setting(("pr_filters",), picked or None)
        except config.ConfigError as e:
            self._notify(str(e), True)
            return
        self.cfg.pr_filters = tuple(picked)
        self._refresh_org()

    def _toggle_team(self) -> None:
        on = self.tab_actions["toggle_team"].isChecked()
        label = "Loading team review requests, this takes a few seconds" if on else "Loading review requests"
        if self._start(label, []) is None:
            self.tab_actions["toggle_team"].setChecked(not on)  # another batch is running: nothing changed
            return
        self.run(self.wb.toggle_team(on))

    def _act(self, action_id: str) -> None:
        """Run an action on the selected line, from the toolbar or a letter key; say why when it cannot."""
        line = self._line()
        if not line:
            return
        title, actions, run = line
        reason = next((r for _, i, _, r in actions if i == action_id), "not for this line")
        if reason:
            self._notify(f"{title}: {reason}", True)
        else:
            run(action_id)

    def _pr_action(self, action_id: str, item: ReviewItem, st: RepoState | None) -> None:
        if action_id == "web":
            webbrowser.open(item.pr.url)
        elif action_id == "checkout" and st:
            if self._start(f"Checking out #{item.pr.number}", [st.path]) is not None:
                self.run(self.wb.checkout(item, st))
        elif st:  # open folder, open in the IDE: the same as for the checkout
            self._repo_action(action_id, st)

    def _pack_action(self, action_id: str, entry: PackEntry) -> None:
        if action_id == "clone":
            if self._start(f"Cloning {entry.repo}", []) is not None:
                self.run(self.wb.clone(entry))
        elif action_id == "web":
            if entry.web_url:
                webbrowser.open(entry.web_url)
        elif action_id == "refresh":
            self.refresh_pack()
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
        elif action_id == "cleanup":
            self._cleanup([st])
        elif action_id in ("keep", "main_branch", "group", "hide"):
            self._repo_setting(action_id, st)
        elif action_id == "ide":
            ide = config.find_ide(self.cfg)
            if not ide or not config.runnable(ide):
                why = f"{ide} cannot be found." if ide else "No IntelliJ launcher found (Toolbox script, idea on PATH)."
                box = QMessageBox(QMessageBox.Icon.Warning, "NH Workbench", why, parent=self)
                box.setInformativeText("Set it in Settings, or turn the IDE actions off there.")
                go = box.addButton("Open Settings", QMessageBox.ButtonRole.AcceptRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                if box.clickedButton() is go:
                    self.open_settings(focus="ide")
                return
            try:
                proc.launch(ide, str(st.path))
            except OSError as e:
                self._notify(f"Could not start {ide}: {e}", True)
                return
            self._notify(f"Opening {st.name}")


def ask_root(missing: Path) -> Path | None:
    """The folder with the checkouts on a first start, the likely one offered first as the terminal UI does."""
    guess = config.suggest_root()
    if guess:
        box = QMessageBox(QMessageBox.Icon.Question, "NH Workbench", f'"{missing}" does not exist.')
        box.setInformativeText(f"Use {guess} as the folder with your checkouts (one repo per subfolder)?")
        use = box.addButton(f"Use {guess.name}", QMessageBox.ButtonRole.AcceptRole)
        other = box.addButton("Choose another...", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(use)
        box.exec()
        if box.clickedButton() is use:
            return guess
        if box.clickedButton() is not other:
            return None
    folder = QFileDialog.getExistingDirectory(None, "Folder with your checkouts", str(guess or Path.home()))
    return Path(folder) if folder else None


def setup_logging(verbose: bool = False) -> Path:
    """Log to gui.log in the cache folder: warnings and errors, everything with -v. Started from a launcher, the
    desktop UI has no console, so errors would be lost otherwise, unhandled ones included."""
    folder = config.cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "gui.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    if verbose:
        root.setLevel(logging.DEBUG)

    def unhandled(kind, error, trace) -> None:
        log.critical("unhandled error", exc_info=(kind, error, trace))
        sys.__excepthook__(kind, error, trace)  # and on the console too, when there is one

    sys.excepthook = unhandled  # also what PySide6 calls for an error in a slot
    return path


def run(cfg: Config, root_override: Path | None = None, verbose: bool = False) -> int:
    setup_logging(verbose)
    if sys.platform == "win32":  # its own taskbar entry and icon, not Python's
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NHWorkbench.Desktop")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("NH Workbench")
    app.setStyle("Fusion")  # looks the same on Windows, Linux and macOS
    gui_theme.apply(app, gui_theme.theme_name(cfg.theme), cfg.font_size)
    if not cfg.root.is_dir():
        folder = ask_root(cfg.root)
        if not folder:
            return 1
        cfg.root = folder
        try:
            config.save_setting(("root",), str(folder))
        except config.ConfigError as e:
            QMessageBox.warning(None, "NH Workbench", f"Could not save the folder, it is asked again next time: {e}")
    window = MainWindow(cfg, root_override)
    window.show()
    return app.exec()


def main() -> None:
    """Entry point of nh-workbench-desktop and the desktop launchers: errors show in a window, not a console."""
    if config.move_old_dirs():  # as the CLI does: the tool was called gtnh-devenv-tools before
        skill.refresh_installed()
    try:
        cfg = config.load()
    except config.ConfigError as e:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "NH Workbench", str(e))
        app.quit()
        raise SystemExit(2) from e
    raise SystemExit(run(cfg))


if __name__ == "__main__":  # python -m nh_workbench.gui, as the desktop launchers start it
    main()
