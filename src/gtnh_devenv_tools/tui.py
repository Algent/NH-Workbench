import webbrowser
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static, Tab, Tabs

from . import config, git, github, ops, pack, proc, render
from .config import Config
from .models import PackEntry, RepoState

VIEWS: dict[str, tuple[str, Callable[[RepoState], bool] | None]] = {
    "all": ("All", lambda st: True),
    "off": ("Off main", lambda st: st.off_main),
    "behind": ("Behind", lambda st: st.is_behind),
    "dirty": ("Dirty", lambda st: st.dirty),
    "branches": ("Branches", lambda st: bool(st.extra_branches)),
    "other": ("Other", lambda st: st.group != "gtnh"),
    "pack": ("Pack", None),
}
REPO_COLUMNS = ("Repo", "Group", "Branch", "State", "Sync", "Pack", "Result", "Last commit")
PACK_COLUMNS = ("Mod", "Kind", "Daily", "Local", "Source")


def repo_cells(st: RepoState) -> list[Text]:
    return [
        Text(st.name, "bold"),
        Text(st.group, "dim"),
        render.branch_text(st),
        render.state_text(st),
        render.sync_text(st),
        Text(st.pack, "dim"),
        render.note_text(st.note),
        render.last_text(st),
    ]


def repo_detail(st: RepoState) -> RenderableType:
    head = Text.assemble(
        (st.name, "bold"), "  ", (st.slug or "no GitHub remote", "dim"), "  main: ", (st.tracked or "?", "green")
    )
    head.append(f"  {st.path}", "dim")
    if not st.extra_branches:
        return Group(head, Text("no other local branches", "dim"))
    table = Table(box=None, pad_edge=False, show_header=False)
    for _ in range(4):
        table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, overflow="ellipsis")
    for br in st.extra_branches:
        table.add_row(*render.branch_row(st, br))
    return Group(head, table)


def pack_detail(entry: PackEntry) -> RenderableType:
    lines = [Text.assemble((entry.name, "bold"), f"  {entry.kind}  daily {entry.version}")]
    lines.append(Text(f"licence: {entry.license or 'unknown'}   {entry.url or ''}", "dim"))
    if entry.local:
        lines.append(Text(f"checked out at {entry.local.path}", "green"))
    elif entry.status == "missing":
        lines.append(Text(f"press c to clone {entry.repo}" + (f" @ {entry.branch}" if entry.branch else ""), "yellow"))
    return Group(*lines)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, title: str, body: RenderableType) -> None:
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._title, id="dialog-title")
            with VerticalScroll(id="dialog-body"):
                yield Static(self._body)
            yield Static(Text.assemble(("y", "bold"), " yes    ", ("n", "bold"), " no"))

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class RepoApp(App[None]):
    CSS_PATH = "tui.tcss"
    TITLE = "gtnh-dev"
    BINDINGS = [
        Binding("f", "fetch", "Fetch all"),
        Binding("p", "pull", "Pull safe"),
        Binding("F", "fetch(True)", "Fetch one", show=False),
        Binding("P", "pull(True)", "Pull one", show=False),
        Binding("r", "rescan", "Rescan"),
        Binding("slash", "filter", "Filter"),
        Binding("o", "open_ide", "IDE"),
        Binding("g", "open_web", "GitHub"),
        Binding("d", "cleanup", "Cleanup"),
        Binding("c", "clone", "Clone"),
        Binding("tab", "cycle_view(1)", "Next view", priority=True),
        Binding("shift+tab", "cycle_view(-1)", show=False, priority=True),
        Binding("escape", "clear_filter", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.states: dict[str, RepoState] = {}
        self.prs = github.PrData()
        self.entries: list[PackEntry] = []
        self.view = "all"
        self.needle = ""
        self.busy = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tabs(*(Tab(label, id=f"view-{key}") for key, (label, _) in VIEWS.items()))
        yield Input(placeholder="Filter by name, Enter to keep, Esc to clear", id="filter")
        yield DataTable(id="repos", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="pack", cursor_type="row", zebra_stripes=True)
        yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Tabs).can_focus = False
        for table_id, columns in (("#repos", REPO_COLUMNS), ("#pack", PACK_COLUMNS)):
            table = self.query_one(table_id, DataTable)
            for column in columns:
                table.add_column(column, key=column)
        self.query_one("#filter").display = False
        self.query_one("#pack").display = False
        self.query_one("#repos").focus()
        self.sub_title = str(self.cfg.root)
        self.load_everything()

    # --- data -----------------------------------------------------------------

    @work(exclusive=True, group="load")
    async def load_everything(self) -> None:
        if not await self._batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), reset=True):
            return
        data = await github.fetch_prs(self.states.values())
        if data.error:
            self.notify(f"PR status unavailable: {data.error}", severity="warning")
        else:
            self.prs = data
            github.attach(self.states.values(), data)
        try:
            self.entries = await pack.load(self.cfg)
        except pack.PackError as e:
            self.notify(str(e), severity="warning")
        self._refresh()

    async def _batch(
        self,
        label: str,
        worker: Callable[[Path], Awaitable[RepoState]],
        paths: list[Path] | None = None,
        reset: bool = False,
    ) -> bool:
        if self.busy:
            self.notify(f"{self.busy} is still running", severity="warning")
            return False
        self.busy = label
        try:
            if paths is None:
                paths = ops.discover(self.cfg)
            if reset:
                keep = {str(p) for p in paths}
                self.states = {k: v for k, v in self.states.items() if k in keep}
            done = 0

            def on_done(st: RepoState) -> None:
                nonlocal done
                done += 1
                github.attach([st], self.prs)
                self.states[str(st.path)] = st
                self._update_row(st)
                self.sub_title = f"{label} {done}/{len(paths)}"

            await ops.run_all(paths, worker, self.cfg.concurrency, on_done)
        finally:
            self.busy = ""
        self._refresh()
        return True

    # --- rendering ------------------------------------------------------------

    def _table(self) -> DataTable:
        return self.query_one("#pack" if self.view == "pack" else "#repos", DataTable)

    def _cursor_key(self) -> str | None:
        table = self._table()
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _selected_repo(self) -> RepoState | None:
        key = self._cursor_key()
        if self.view == "pack":
            entry = self._selected_entry()
            return entry.local if entry else None
        return self.states.get(key) if key else None

    def _selected_entry(self) -> PackEntry | None:
        key = self._cursor_key()
        return next((e for e in self.entries if e.name == key), None) if self.view == "pack" else None

    def _fill(self, table: DataTable, rows: list[tuple[str, list[Text]]]) -> None:
        selected = self._cursor_key() if table is self._table() else None
        table.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key)
        index = next((i for i, (key, _) in enumerate(rows) if key == selected), 0)
        if rows:
            table.move_cursor(row=index)

    def _update_row(self, st: RepoState) -> None:
        table = self.query_one("#repos", DataTable)
        key = str(st.path)
        if key in table.rows:
            for column, cell in zip(REPO_COLUMNS, repo_cells(st), strict=True):
                table.update_cell(key, column, cell, update_width=True)

    def _refresh(self) -> None:
        if self.entries:
            pack.link_local(self.entries, self.states.values())
        states = sorted(self.states.values(), key=lambda st: st.name.lower())
        for key, (label, wanted) in VIEWS.items():
            count = len(self.entries) if wanted is None else sum(1 for st in states if wanted(st))
            self.query_one(f"#view-{key}", Tab).label = f"{label} {count}"

        if self.view == "pack":
            shown = [e for e in self.entries if self.needle in e.name.lower()]
            self._fill(self.query_one("#pack", DataTable), [(e.name, render.pack_row(e)) for e in shown])
        else:
            wanted = VIEWS[self.view][1]
            shown = [st for st in states if wanted(st) and self.needle in st.name.lower()]
            self._fill(self.query_one("#repos", DataTable), [(str(st.path), repo_cells(st)) for st in shown])
        if not self.busy:
            self.sub_title = f"{self.cfg.root}  ·  {render.summary(states).plain}"
        self._update_detail()

    def _update_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        entry, st = self._selected_entry(), self._selected_repo()
        if entry:
            detail.update(pack_detail(entry))
        elif st:
            detail.update(repo_detail(st))
        else:
            detail.update(Text("nothing selected", "dim"))

    # --- events ---------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self._table():
            self._update_detail()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        self.view = (event.tab.id or "view-all").removeprefix("view-")
        self.query_one("#repos").display = self.view != "pack"
        self.query_one("#pack").display = self.view == "pack"
        if not isinstance(self.focused, Input):
            self._table().focus()
        self._refresh()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.needle = event.value.strip().lower()
        self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._table().focus()

    # --- actions --------------------------------------------------------------

    def _modal(self) -> bool:
        return len(self.screen_stack) > 1

    def action_cycle_view(self, step: int) -> None:
        if self._modal():
            return
        keys = list(VIEWS)
        self.query_one(Tabs).active = f"view-{keys[(keys.index(self.view) + step) % len(keys)]}"

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.value = ""
        box.display = False
        self._table().focus()

    def action_rescan(self) -> None:
        self.load_everything()

    def action_fetch(self, one: bool = False) -> None:
        self._operate("Fetching", lambda p: ops.fetch_repo(self.cfg, p), one)

    def action_pull(self, one: bool = False) -> None:
        self._operate("Pulling", lambda p: ops.pull_repo(self.cfg, p), one)

    @work(group="batch")
    async def _operate(self, label: str, worker: Callable[[Path], Awaitable[RepoState]], one: bool) -> None:
        paths = None
        if one:
            st = self._selected_repo()
            if not st:
                return
            paths = [st.path]
        await self._batch(label, worker, paths)

    def action_open_ide(self) -> None:
        st = self._selected_repo()
        ide = config.find_ide(self.cfg)
        if not st:
            return
        if not ide:
            self.notify("No IDE launcher found, set ide_command in the config", severity="warning")
            return
        proc.launch(ide, str(st.path))
        self.notify(f"Opening {st.name}")

    def action_open_web(self) -> None:
        entry, st = self._selected_entry(), self._selected_repo()
        url = None
        if st:
            pr = st.current.pr if st.current else None
            url = pr.url if pr else (f"https://github.com/{st.slug}" if st.slug else None)
        elif entry:
            url = f"https://github.com/{entry.repo}" if entry.repo else entry.url
        if url:
            webbrowser.open(url)

    @work(group="dialog")
    async def action_cleanup(self) -> None:
        if self._modal() or self.busy:
            return
        safe, unpushed = await ops.cleanup_candidates(self.states.values())
        if not safe:
            kept = f" ({len(unpushed)} merged ones hold local commits)" if unpushed else ""
            self.notify(f"No merged branches to clean up{kept}")
            return
        body = Text("\n").join(
            Text.assemble((st.name, "bold"), f"  {br.name}", (f"  PR #{br.pr.number}", "dim")) for st, br in safe
        )
        if not await self.push_screen_wait(ConfirmScreen(f"Delete {len(safe)} merged local branches?", body)):
            return
        restore, failed = [], 0
        for st, br in safe:
            res = await git.delete_branch(st.path, br.name, br.sha)
            if res.ok:
                restore.append(f'{datetime.now():%Y-%m-%d %H:%M} git -C "{st.path}" branch {br.name} {br.sha}')
            else:
                failed += 1
        log_file = config.cache_dir() / "deleted-branches.log"
        if restore:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as f:
                f.write("\n".join(restore) + "\n")
        await self._batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), list({st.path for st, _ in safe}))
        note = f"Deleted {len(restore)} branches, restore commands in {log_file}"
        self.notify(note + (f"; {failed} failed" if failed else ""), severity="warning" if failed else "information")

    @work(group="clone")
    async def action_clone(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self.notify("Clone works from the Pack view")
            return
        if entry.local:
            return
        self.notify(f"Cloning {entry.repo or entry.name}...")
        res = await pack.clone(entry, self.cfg.root)
        if not res.ok:
            self.notify(f"{entry.name}: {res.message}", severity="error")
            return
        await self._batch(
            "Scanning", lambda p: ops.scan_repo(self.cfg, p), [self.cfg.root / entry.repo.partition("/")[2]]
        )
        self.notify(f"Cloned {entry.repo}")
