import asyncio
import time
import webbrowser
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from . import config, github, ops, pack, proc, render
from .config import Config
from .models import CloneJob, PackEntry, PackInfo, RepoState, ReviewItem

VIEWS: dict[str, tuple[str, Callable[[RepoState], bool] | None]] = {
    "all": ("All", lambda st: True),
    "off": ("Off main", lambda st: st.off_main),
    "behind": ("Behind", lambda st: st.is_behind),
    "dirty": ("Dirty", lambda st: st.dirty),
    "branches": ("Branches", lambda st: bool(st.extra_branches)),
    "other": ("Other", lambda st: st.group != "gtnh"),
    "mine": ("My PRs", None),
    "reviews": ("Reviews", None),
    "org": ("All PRs", None),
    "pack": ("Pack", None),
    "tools": ("Tools", None),
}
SORTS: dict[str, tuple[str, Callable[[RepoState], object]]] = {
    "name": ("name", ops.by_repo),
    "recent": ("last commit", lambda st: -(st.date.timestamp() if st.date else 0)),
    "behind": ("most behind", lambda st: -(st.behind + st.main_behind)),
    "branch": ("off main first", lambda st: (not st.off_main, (st.head or "").lower())),
}
HEADER_SORT = {"Repo": "name", "Last commit": "recent", "Sync": "behind", "Branch": "branch"}
# The PR lists keep their own order: "updated" is the order they arrive in (for you first, then newest).
PR_SORTS = render.PR_SORTS
PR_HEADER_SORT = {"Updated": "updated", "Size": "small", "Repo": "repo"}
REPO_ONLY_ACTIONS = {"fetch", "pull", "to_main", "cleanup"}  # hidden on the Reviews and Pack tabs
# views with their own table; the rest share #repos
TABLES = {"pack": "#pack", "reviews": "#reviews", "mine": "#mine", "org": "#org", "tools": "#tools"}
PR_VIEWS = ("reviews", "mine", "org")  # views listing pull requests rather than repos
REPO_COLUMNS = ("Repo", "Group", "Branch", "State", "Sync", "Daily", "Release", "Result", "Last commit")
PACK_COLUMNS = ("Mod", "Kind", "Daily", "Release", "Local", "Source")
TOOL_COLUMNS = ("Tool", "What it does")
# (key, name, what it does, longer help for the detail pane)
TOOLS = [
    (
        "spark",
        "spark profiles",
        "Read a spark.lucko.me link (a lag report) as a short text report",
        "Paste a spark link or code: server stats, the worst minutes, where the time goes. Enter on a method"
        " shows what it calls, c who calls it, y copies the report for an LLM. Same as `gtnh-dev tools spark`.",
    ),
]
NOT_ON_TOOLS = {"sort", "open_ide", "open_web", "filter", "clone"}
BACKPORTS_SHOWN = 4  # release-branch commit subjects listed in the detail pane


def repo_cells(st: RepoState) -> list[Text]:
    return [
        render.name_text(st),
        Text(st.group, "dim"),
        render.branch_text(st),
        render.state_text(st),
        render.sync_text(st),
        render.daily_text(st),
        render.release_text(st),
        render.note_text(st.note),
        render.last_text(st),
    ]


def pack_line(st: RepoState, info: PackInfo) -> Text:
    """Where a checkout stands in the pack: daily version, released version, release branch activity."""
    text = Text.assemble(("pack  ", "dim"), "daily ", (st.daily or "—", "bold"))
    if st.daily_ahead:
        text.append(f" (+{st.daily_ahead} on {st.tracked} since)", "cyan")
    text.append(f"   {info.release or 'release'} ").append(st.release or "—", "bold")
    branch = pack.release_branch(info.release) if info.release else ""
    if branch in st.release_branches and st.release:
        if st.release_ahead is None:
            text.append(f"   {branch}: tag {st.release} not found locally", "dim")
        else:
            style = "cyan" if st.release_ahead else "dim"
            text.append(f"   {branch}: {st.release_ahead} commits since {st.release}", style)
    if st.release_delta:
        text.append(f"   {st.tracked}: Δ{st.release_delta} since {st.release}", render.delta_style(st.release_delta))
    return text


def repo_detail(st: RepoState, info: PackInfo | None = None) -> RenderableType:
    head = Text.assemble(
        (st.name, "bold"), "  ", (st.slug or "no GitHub remote", "dim"), "  main: ", (st.tracked or "?", "green")
    )
    head.append(f"  {st.path}", "dim").append("  Enter: actions", "dim italic")
    lines: list[RenderableType] = [head]
    if st.subject or st.date:
        lines.append(Text.assemble(("last commit  ", "dim"), (render.ago(st.date), "dim"), f"  {st.subject}"))
    if st.worktree_of:
        lines.append(Text(f"working tree of {st.worktree_of}", "dim"))
    for tree in st.worktrees:
        lines.append(Text(f"other working tree: {tree.path}  on {tree.branch or 'a detached commit'}", "dim"))
    if info and st.in_pack:
        lines.append(pack_line(st, info))
        for subject in st.release_commits[:BACKPORTS_SHOWN]:
            lines.append(Text(f"        {render.clip(subject, 100)}", "dim cyan"))
        if len(st.release_commits) > BACKPORTS_SHOWN:
            lines.append(Text(f"        ... {len(st.release_commits) - BACKPORTS_SHOWN} more", "dim"))
    if not st.extra_branches:
        return Group(*lines, Text("no other local branches", "dim"))
    table = Table(box=None, pad_edge=False, show_header=False)
    for _ in range(4):
        table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, overflow="ellipsis")
    for br in st.extra_branches:
        table.add_row(*render.branch_row(st, br))
    return Group(*lines, table)


def review_detail(item: ReviewItem, local: RepoState | None) -> RenderableType:
    head = Text.assemble((f"{item.repo} ", "bold"), render.pr_text(item.pr), f"  by {item.author}")
    where = Text(f"checked out at {local.path}", "green") if local else Text("not checked out", "dim")
    hint = Text(f"{item.pr.url}   Enter or w: open in the browser   t: show or hide team requests", "dim")
    return Group(head, Text(item.pr.title), where, hint)


def org_detail(item: ReviewItem, mark: str, local: RepoState | None) -> RenderableType:
    head = Text.assemble((f"{item.repo} ", "bold"), render.pr_text(item.pr), f"  by {item.author}")
    concern = {"author": "your PR", "requested": "your review is requested", "reviewed": "you reviewed it"}
    where = Text(f"checked out at {local.path}", "green") if local else Text("not checked out", "dim")
    if mark:
        where = Text.assemble((concern[mark], "bold"), "   ", where)
    hint = Text(f"{item.pr.url}   Enter or w: open   c: check out   t: drafts   v: show only   r: search again", "dim")
    return Group(head, Text(item.pr.title), where, hint)


def mine_detail(item: ReviewItem, local: tuple[RepoState, str] | None) -> RenderableType:
    head = Text.assemble((f"{item.repo} ", "bold"), render.pr_text(item.pr))
    if local:
        where = Text(f"branch {local[1]} in {local[0].path}", "green")
    else:
        where = Text("branch not checked out here", "dim")
    hint = Text(f"{item.pr.url}   Enter or w: open in the browser", "dim")
    return Group(head, Text(item.pr.title), where, hint)


def pack_detail(entry: PackEntry, info: PackInfo) -> RenderableType:
    release = f"{info.release or 'release'} {entry.release_version or '—'}"
    versions = f"  {entry.kind}  daily {entry.version or '—'}  {release}"
    lines = [render.pack_header(info), Text.assemble((entry.name, "bold"), versions)]
    lines.append(Text(f"licence: {entry.license or 'unknown'}   {entry.url or ''}", "dim"))
    if entry.dev_status or entry.note:
        lines.append(Text(" · ".join(part for part in (entry.dev_status, entry.note) if part), "dim italic"))
    if entry.local:
        lines.append(Text(f"checked out at {entry.local.path}", "green"))
    elif entry.status == "missing":
        lines.append(Text(f"press c to clone {entry.repo}" + (f" @ {entry.branch}" if entry.branch else ""), "yellow"))
    return Group(*lines)


class CloseButton(Static):
    """Clickable way out of a dialog, for terminals that swallow Esc."""

    def __init__(self) -> None:
        super().__init__("✕", id="close")

    def on_click(self) -> None:
        self.screen.dismiss(None)


def dialog_head(title: RenderableType) -> Horizontal:
    return Horizontal(Static(title, id="dialog-title"), CloseButton(), id="dialog-head")


class ConfirmScreen(ModalScreen[bool]):
    """Yes or no, with an optional checkbox (toggled with s) read back from .checked."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,q,escape", "answer(False)", "No"),
        Binding("s", "toggle_option", show=False),
    ]
    AUTO_FOCUS = "#no"  # Enter on its own must never confirm

    def __init__(self, title: str, body: RenderableType, option: str | None = None, checked: bool = True) -> None:
        super().__init__()
        self._title, self._body, self._option, self.checked = title, body, option, checked

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield dialog_head(self._title)
            with VerticalScroll(id="dialog-body"):
                yield Static(self._body)
            if self._option:
                yield Checkbox(f"{self._option} (s)", self.checked, id="option")
            with Horizontal(id="dialog-buttons"):
                yield Button("Yes (y)", id="yes", variant="warning")
                yield Button("No (n)", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self.checked = event.value

    def action_toggle_option(self) -> None:
        if self._option:
            self.query_one("#option", Checkbox).toggle()


class PromptScreen(ModalScreen[str | None]):
    """Ask for one line of text. Dismisses with the text, or None when cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, value: str = "", hint: str = "") -> None:
        super().__init__()
        self._title, self._value, self._hint = title, value, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield dialog_head(self._title)
            yield Input(value=self._value, id="prompt")
            yield Static(Text(f"{self._hint}  Enter to save, Esc or ✕ to cancel".strip(), "dim"))

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()  # keep it away from the app's filter box handler

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


def repo_actions(st: RepoState, ide_off: bool = False) -> list[tuple[str, str, str, str | None]]:
    """(hotkey, id, label, reason it is unavailable) for the single-repo menu."""
    main = st.tracked or "main"
    merged = sum(1 for b in st.extra_branches if b.name != st.head and not b.keep and b.pr and b.pr.state == "MERGED")
    kept = sum(1 for b in st.branches if b.keep)
    pr = st.current.pr if st.current else None
    if not st.off_main:
        no_switch = f"already on {main}" if st.tracked else "no main branch known"
    else:
        elsewhere = st.checked_out_elsewhere(st.tracked)
        no_switch = "uncommitted changes" if st.dirty else None
        if elsewhere:
            no_switch = f"{main} is checked out in {elsewhere.path.name}"
    return [
        ("f", "fetch", "Fetch", None),
        ("p", "pull", f"Pull: fast-forward {main} if safe", None),
        ("m", "main", f"Switch to {main} and pull", no_switch),
        ("d", "cleanup", f"Delete merged branches ({merged})", None if merged else "none merged"),
        ("k", "keep", f"Protect branches from cleanup ({kept})", None),
        ("o", "ide", "Open in IntelliJ", "turned off in Extras" if ide_off else None),
        ("w", "web", f"Open PR #{pr.number}" if pr else "Open on GitHub", None if st.slug else "no GitHub remote"),
        ("e", "folder", "Open folder", None),
        ("b", "main_branch", f"Set main branch (now {main})", None),
        ("u", "group", f"Set group (now {st.group})", None),
        ("h", "hide", "Hide this repo from the tool", None),
    ]


class MenuScreen(ModalScreen[str | None]):
    """A list of (hotkey, id, label, reason it is unavailable) actions; dismisses with the chosen id."""

    # Esc alone is not enough: JetBrains terminals grab it to refocus the editor.
    BINDINGS = [Binding("escape,q,backspace,left", "close", "Close")]

    def __init__(self, title: RenderableType, actions: list[tuple[str, str, str, str | None]], info=None) -> None:
        super().__init__()
        self._title, self._actions, self._info = title, actions, info

    def compose(self) -> ComposeResult:
        options = []
        for key, id_, label, why in self._actions:
            prompt = Text.assemble((key, "bold"), f"  {label}", (f"  ({why})" if why else "", "dim"))
            options.append(Option(prompt, id=id_, disabled=bool(why)))
        with Vertical(id="dialog"):
            yield dialog_head(self._title)
            if self._info is not None:
                yield Static(self._info, id="dialog-info")
            yield OptionList(*options, id="menu")
            yield Static(Text("Enter or the letter to run, q or Esc to close", "dim"))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event: Key) -> None:
        choice = next((id_ for key, id_, _, why in self._actions if key == event.key and not why), None)
        if choice:
            event.stop()
            self.dismiss(choice)

    def action_close(self) -> None:
        self.dismiss(None)


class RepoMenu(MenuScreen):
    def __init__(self, st: RepoState, ide_off: bool = False) -> None:
        super().__init__(Text.assemble((st.name, "bold"), "  ", render.branch_text(st)), repo_actions(st, ide_off))
        self._st = st


class ExtrasMenu(MenuScreen):
    def __init__(self, cfg: Config, info: PackInfo | None) -> None:
        lines = [Text(f"Root: {cfg.root}", "dim"), Text(f"Config: {config.config_path()}", "dim")]
        if info:
            lines.append(render.pack_header(info))
        closed = sum(1 for entry in info.entries if entry.closed) if info else 0
        every = f"every {cfg.refresh_minutes} min" if cfg.refresh_minutes else "off"
        ide_now = "off" if cfg.ide_off else cfg.ide_command or "found automatically"
        other = "HTTPS" if cfg.clone_protocol == "ssh" else "SSH"
        actions = [
            ("a", "auto", f"Auto-refresh interval (now {every})", None),
            ("f", "refresh_fetch", f"Fetch during auto-refresh (now {'on' if cfg.refresh_fetch else 'off'})", None),
            ("i", "ide", f"IntelliJ launcher (now {ide_now})", None),
            ("s", "protocol", f"Clone over {other} (now {cfg.clone_protocol.upper()})", None),
            ("m", "clone_many", "Clone missing repos: pack mods, org repos", None),
            ("r", "refresh", "Refresh pack data now (otherwise hourly)", None),
            ("o", "root", "Change the repo folder", None),
            ("u", "unhide", f"Show a hidden repo again ({len(cfg.hidden)})", None if cfg.hidden else "none hidden"),
            ("k", "unkeep", f"Unprotect a kept branch ({len(cfg.kept)})", None if cfg.kept else "none protected"),
            ("c", "config", "Open config folder", None),
            ("l", "logs", "Open logs and cache folder", None),
            ("j", "jars", f"Download closed-source jars ({closed})", None if closed else "pack data not loaded"),
            ("b", "purge_jars", f"Delete built jars (build/libs) of {cfg.org} repos", None),
        ]
        super().__init__(Text("Extras", "bold"), actions, Group(*lines))


class RepoApp(App[None]):
    CSS_PATH = "tui.tcss"
    TITLE = "gtnh-dev"
    BINDINGS = [
        Binding("f", "fetch", "Fetch all"),
        Binding("p", "pull", "Pull safe"),
        Binding("F", "fetch(True)", "Fetch one", show=False),
        Binding("P", "pull(True)", "Pull one", show=False),
        Binding("r", "rescan", "Rescan"),
        Binding("r", "reload_org", "Search again"),
        Binding("slash", "filter", "Filter"),
        Binding("o", "open_ide", "IDE"),
        Binding("w", "open_web", "Web"),
        Binding("m", "to_main", "To main"),
        Binding("d", "cleanup", "Cleanup"),
        Binding("c", "clone", "Clone"),
        Binding("c", "checkout", "Check out"),
        Binding("s", "sort", "Sort"),
        Binding("t", "toggle_team", "Team reviews"),
        Binding("t", "toggle_drafts", "Drafts"),
        Binding("v", "pr_filters", "Show only"),
        Binding("x", "extras", "Extras"),
        Binding("C", "open_folder('config')", "Config dir", show=False),
        Binding("L", "open_folder('logs')", "Logs dir", show=False),
        Binding("tab", "cycle_view(1)", "Next view", priority=True),
        Binding("shift+tab", "cycle_view(-1)", show=False, priority=True),
        Binding("escape", "clear_filter", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: Config, root_override: Path | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.root_override = root_override  # --root on the command line wins over the config file
        self.sort = "name"
        self.pr_sort = "updated"
        self.team_reviews = False
        self.states: dict[str, RepoState] = {}
        self.prs = github.PrData()
        self.entries: list[PackEntry] = []
        self.pack_info: PackInfo | None = None
        self.reviews: list[ReviewItem] = []
        self.mine: list[ReviewItem] = []
        self.org: github.OrgPrs | None = None  # loaded the first time the All PRs tab opens
        self.org_drafts = False
        self.last_loaded = time.monotonic()
        self.view = "all"
        self.needle = ""
        self.busy = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tabs(*(Tab(label, id=f"view-{key}") for key, (label, _) in VIEWS.items()))
        hint = "Filter by name (on PR tabs <200 keeps PRs under 200 changed lines): Enter keeps it, Esc removes it"
        yield Input(placeholder=hint, id="filter")
        yield DataTable(id="repos", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="pack", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="reviews", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="mine", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="org", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="tools", cursor_type="row", zebra_stripes=True)
        yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Tabs).can_focus = False
        layout = (
            ("#repos", REPO_COLUMNS),
            ("#pack", PACK_COLUMNS),
            ("#reviews", render.REVIEW_COLUMNS),
            ("#mine", render.MINE_COLUMNS),
            ("#org", render.ORG_COLUMNS),
            ("#tools", TOOL_COLUMNS),
        )
        for table_id, columns in layout:
            table = self.query_one(table_id, DataTable)
            for column in columns:
                table.add_column(column, key=column)
        self.query_one("#filter").display = False
        for table_id in TABLES.values():
            self.query_one(table_id).display = False
        self.query_one("#repos").focus()
        self.sub_title = str(self.cfg.root)
        if self.cfg.theme in self.available_themes:
            self.theme = self.cfg.theme
        self.theme_changed_signal.subscribe(self, self._save_theme)
        if self.cfg.root.is_dir():
            self.load_everything()
        else:
            self._first_root()
        # Checked every minute, so a new interval set from Extras applies without a restart.
        self.set_interval(60, self.auto_refresh)

    def _save_theme(self, theme) -> None:
        """Keep the theme picked in the command palette for the next start."""
        if theme.name == self.cfg.theme:
            return
        try:
            config.save_setting(("theme",), theme.name)
        except config.ConfigError as e:
            self.notify(str(e), severity="error")
            return
        self.cfg.theme = theme.name

    def auto_refresh(self) -> None:
        """Rescan like `r` does once the interval has passed, unless something else is going on."""
        minutes = self.cfg.refresh_minutes
        if not minutes or self.busy or self._modal():
            return
        if time.monotonic() - self.last_loaded >= minutes * 60:
            self.load_everything(fetch=self.cfg.refresh_fetch)

    # --- data -----------------------------------------------------------------

    @work(exclusive=True, group="load")
    async def load_everything(self, fetch: bool = False) -> None:
        self.last_loaded = time.monotonic()
        if fetch:
            loaded = await self._batch("Fetching", lambda p: ops.fetch_repo(self.cfg, p), reset=True)
        else:
            loaded = await self._batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), reset=True)
        if not loaded:
            return
        # Independent sources: none of them should hold the others up.
        await asyncio.gather(self._load_prs(), self._load_reviews(quiet=True), self._load_mine(), self._load_pack())

    async def _load_mine(self) -> None:
        items, error = await github.fetch_mine()
        if items or not error:
            self.mine = items
            self._refresh()

    async def _load_prs(self) -> None:
        data = await github.fetch_prs(self.states.values())
        if data.error:
            self.notify(f"PR status unavailable: {data.error}", severity="warning")
        if data.prs or data.canonical or not data.error:
            self.prs = data
            github.attach(self.states.values(), data)
            await self._relink()  # canonical names can change which pack mod a repo matches
            self._refresh()

    async def _load_reviews(self, quiet: bool = False) -> None:
        items, error = await github.fetch_reviews(self.team_reviews)
        if error and not quiet:
            self.notify(f"Review requests: {error}", severity="warning")
        if items or not error:
            self.reviews = items
            self._refresh()

    def _open_org(self) -> None:
        """Show the saved org search at once; search again only when it is missing or old."""
        if self.org is None:
            self.org = github.cached_org(self.cfg.org)
        if self.org is None or time.time() - self.org.fetched > github.ORG_MAX_AGE:
            self.reload_org()

    @work(group="org", exclusive=True)
    async def reload_org(self) -> None:
        self.notify(f"Searching all open PRs of {self.cfg.org}, this takes a while...")
        found = await github.fetch_org(self.cfg.org)
        if found.error:
            self.notify(f"Org PRs: {found.error}", severity="warning")
        if found.items or not found.error:
            self.org = found
            self._refresh()

    def _org_shown(self) -> list[ReviewItem]:
        items = self.org.items if self.org else []
        if not self.org_drafts:
            items = [i for i in items if not i.pr.draft]
        if not self.cfg.pr_filters:
            return items
        local = ops.local_slugs(self.states.values())
        return github.filter_prs(items, self.cfg.pr_filters, local, pack.pack_slugs(self.entries))

    def _org_mark(self, item: ReviewItem) -> str:
        url = item.pr.url
        if any(i.pr.url == url for i in self.mine):
            return "author"
        if any(i.pr.url == url for i in self.reviews):
            return "requested"
        return "reviewed" if self.org and url in self.org.reviewed else ""

    async def _load_pack(self, force: bool = False) -> None:
        try:
            self.pack_info = await pack.load(self.cfg, force)
            self.entries = self.pack_info.entries
            # Rebuild the headers so they name the daily build and the release.
            table = self.query_one("#pack", DataTable)
            labels = render.pack_labels(self.pack_info)
            table.clear(columns=True)
            for column in PACK_COLUMNS:
                table.add_column(labels.get(column, column), key=column)
        except pack.PackError as e:
            self.notify(str(e), severity="warning")
        await self._relink()
        self._refresh()

    async def _relink(self, paths: list[Path] | None = None) -> None:
        """Copy pack versions onto the repos, and recount release-branch commits for the given ones."""
        if not self.pack_info:
            return
        pack.link_local(self.entries, self.states.values())
        keys = self.states if paths is None else [str(p) for p in paths]
        states = [self.states[k] for k in keys if k in self.states]
        branch = pack.release_branch(self.pack_info.release) if self.pack_info.release else ""
        await ops.pack_progress(states, branch, self.cfg.concurrency)

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
                old = self.states.get(str(st.path))
                if old:  # keep the pack columns steady until they are recomputed after the batch
                    st.daily, st.release, st.release_ahead = old.daily, old.release, old.release_ahead
                    st.daily_ahead, st.release_commits = old.daily_ahead, old.release_commits
                    st.release_delta = old.release_delta
                self.states[str(st.path)] = st
                self._update_row(st)
                self.sub_title = f"{label} {done}/{len(paths)}"

            await ops.run_all(paths, worker, self.cfg.concurrency, on_done)
        finally:
            self.busy = ""
        await self._relink(paths)
        self._refresh()
        return True

    # --- rendering ------------------------------------------------------------

    def _table(self) -> DataTable:
        return self.query_one(TABLES.get(self.view, "#repos"), DataTable)

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
        if self.view in PR_VIEWS:
            item = self._selected_review()
            return self._repo_for(item) if item else None
        return self.states.get(key) if key else None

    def _selected_review(self) -> ReviewItem | None:
        """The highlighted pull request on the Reviews or My PRs tab."""
        if self.view not in PR_VIEWS:
            return None
        key = self._cursor_key()
        items = {"reviews": self.reviews, "mine": self.mine, "org": self.org.items if self.org else []}[self.view]
        return next((i for i in items if i.pr.url == key), None)

    def _local_branch(self, item: ReviewItem) -> tuple[RepoState, str] | None:
        """The checkout and local branch a pull request was opened from, when it is here."""
        for st in self.states.values():
            for br in st.branches:
                if br.pr and br.pr.url == item.pr.url:
                    return st, br.name
        return None

    def _repo_for(self, item: ReviewItem) -> RepoState | None:
        local = self._local_branch(item)
        if local:
            return local[0]
        wanted = item.repo.lower()
        # Any remote counts: a PR to an upstream repo still belongs to the fork checked out here.
        for st in sorted(self.states.values(), key=lambda s: s.worktree_of is not None):
            if any((r.slug or "").lower() == wanted for r in st.remotes.values()) or (st.slug or "").lower() == wanted:
                return st
        return None

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
        states = sorted(self.states.values(), key=ops.by_repo)
        states.sort(key=SORTS[self.sort][1])  # stable, so ties stay in name order
        special = {
            "pack": len(self.entries),
            "reviews": sum(1 for item in self.reviews if item.direct),
            "mine": len(self.mine),
            "org": len(self._org_shown()) if self.org else None,  # not searched yet
            "tools": None,
        }
        for key, (label, wanted) in VIEWS.items():
            count = special[key] if wanted is None else sum(1 for st in states if wanted(st))
            self.query_one(f"#view-{key}", Tab).label = label if count is None else f"{label} {count}"

        if self.view == "reviews":
            shown = self._pr_items(self.reviews)
            self._fill(self.query_one("#reviews", DataTable), [(i.pr.url, render.review_row(i)) for i in shown])
        elif self.view == "mine":
            shown = self._pr_items(self.mine)
            rows = []
            for item in shown:
                local = self._local_branch(item)
                rows.append((item.pr.url, render.mine_row(item, local[1] if local else "")))
            self._fill(self.query_one("#mine", DataTable), rows)
        elif self.view == "org":
            shown = self._pr_items(self._org_shown())
            rows = [(i.pr.url, render.org_row(i, self._org_mark(i))) for i in shown]
            self._fill(self.query_one("#org", DataTable), rows)
        elif self.view == "tools":
            rows = [(key, [Text(name, "bold"), Text(what)]) for key, name, what, _ in TOOLS]
            self._fill(self.query_one("#tools", DataTable), rows)
        elif self.view == "pack":
            shown = [e for e in self.entries if self.needle in e.name.lower()]
            self._fill(self.query_one("#pack", DataTable), [(e.name, render.pack_row(e)) for e in shown])
        else:
            wanted = VIEWS[self.view][1]
            shown = [st for st in states if wanted(st) and self.needle in st.name.lower()]
            self._fill(self.query_one("#repos", DataTable), [(str(st.path), repo_cells(st)) for st in shown])
        if not self.busy:
            if self.view in PR_VIEWS:
                order = "" if self.pr_sort == "updated" else f"  ·  PRs sorted by {PR_SORTS[self.pr_sort][0]}"
                if self.view == "org" and self.org:
                    when = datetime.fromtimestamp(self.org.fetched, UTC) if self.org.fetched else None
                    drafts = "with drafts" if self.org_drafts else "no drafts"
                    order += f"  ·  {self.cfg.org} PRs as of {render.ago(when) or 'now'} ago, {drafts}"
                    only = [github.PR_FILTERS[f] for f in self.cfg.pr_filters if f in github.PR_FILTERS]
                    order += f", only {' and '.join(only)}" if only else ""
            else:
                order = "" if self.sort == "name" else f"  ·  sorted by {SORTS[self.sort][0]}"
            self.sub_title = f"{self.cfg.root}  ·  {render.summary(states).plain}{order}"
        self._update_detail()

    def _pr_items(self, items: list[ReviewItem]) -> list[ReviewItem]:
        """PRs matching the filter, in the chosen order. A filter like <200 keeps PRs under 200 changed lines."""
        needle = self.needle
        if needle.startswith("<") and needle[1:].strip().isdigit():
            limit = int(needle[1:])
            shown = [i for i in items if i.pr.size < limit]
        else:
            shown = [i for i in items if needle in f"{i.repo} {i.pr.title} {i.author}".lower()]
        key = PR_SORTS[self.pr_sort][1]
        return sorted(shown, key=key) if key else shown

    def _update_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        if self.view == "tools":
            key = self._cursor_key()
            tool = next((t for t in TOOLS if t[0] == key), None)
            detail.update(Text(tool[3]) if tool else Text("nothing selected", "dim"))
            return
        entry, st, review = self._selected_entry(), self._selected_repo(), self._selected_review()
        if review and self.view == "mine":
            detail.update(mine_detail(review, self._local_branch(review)))
        elif review and self.view == "org":
            detail.update(org_detail(review, self._org_mark(review), st))
        elif review:
            detail.update(review_detail(review, st))
        elif entry and self.pack_info:
            detail.update(pack_detail(entry, self.pack_info))
        elif st:
            detail.update(repo_detail(st, self.pack_info))
        else:
            detail.update(Text("nothing selected", "dim"))

    # --- events ---------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self._table():
            self._update_detail()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        column = event.column_key.value or ""
        if event.data_table.id == "repos" and column in HEADER_SORT:
            self.sort = HEADER_SORT[column]
            self._refresh()
        elif event.data_table.id in PR_VIEWS and column in PR_HEADER_SORT:
            wanted = PR_HEADER_SORT[column]
            # Clicking Size twice flips between smallest and largest first.
            self.pr_sort = "large" if wanted == "small" and self.pr_sort == "small" else wanted
            self._refresh()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self.view == "tools":
            self._open_tool(self._cursor_key())
            return
        if self.view in PR_VIEWS:
            self.action_open_web()
            return
        st = self._selected_repo()
        if st and not st.error and not self._modal():
            self._repo_menu(st)

    @work(group="dialog")
    async def _repo_menu(self, st: RepoState) -> None:
        choice = await self.push_screen_wait(RepoMenu(st, self.cfg.ide_off))
        one = [st.path]
        if choice == "fetch":
            await self._batch("Fetching", lambda p: ops.fetch_repo(self.cfg, p), one)
        elif choice == "pull":
            await self._batch("Pulling", lambda p: ops.pull_repo(self.cfg, p), one)
        elif choice == "main":
            await self._batch("Switching", lambda p: ops.to_main(self.cfg, st), one)
        elif choice == "cleanup":
            await self._cleanup([st])
        elif choice == "ide":
            self._open_ide(st)
        elif choice == "web":
            self._open_web(st, None)
        elif choice == "folder":
            proc.open_path(st.path)
        elif choice == "main_branch":
            hint = "Leave empty to go back to the automatic choice."
            current = self.cfg.repo(st.name).tracked_branch or ""
            branch = await self.push_screen_wait(PromptScreen(f"Main branch for {st.name}", current, hint))
            if branch is None:
                return
            if branch and st.branch(branch) is None:
                self.notify(f"No local branch {branch} yet: pull will say so until it exists", severity="warning")
            self._save_setting(("repos", st.name, "tracked_branch"), branch or None)
        elif choice == "keep":
            hint = "Branch names, separated by commas. Leave empty to protect none."
            current = ", ".join(self.cfg.repo(st.name).keep_branches)
            answer = await self.push_screen_wait(PromptScreen(f"Branches of {st.name} to keep", current, hint))
            if answer is None:
                return
            names = list(dict.fromkeys(n.strip() for n in answer.split(",") if n.strip()))
            missing = [n for n in names if st.branch(n) is None]
            if missing:
                self.notify(f"No local branch {', '.join(missing)}: saved anyway", severity="warning")
            self._save_setting(("repos", st.name, "keep_branches"), names or None)
        elif choice == "group":
            hint = "Leave empty for the automatic group."
            group = await self.push_screen_wait(PromptScreen(f"Group for {st.name}", st.group, hint))
            if group is not None:
                self._save_setting(("repos", st.name, "group"), group or None)
        elif choice == "hide":
            body = Text(f"Adds ignore = true for {st.name} to the config file.\nTo undo, edit the file (Extras, c).")
            if await self.push_screen_wait(ConfirmScreen(f"Hide {st.name} from the tool?", body)):
                self._save_setting(("repos", st.name, "ignore"), True)

    def _save_setting(
        self, keys: tuple[str, ...], value: str | bool | int | list | None, rescan: bool = True, quiet: bool = False
    ) -> bool:
        """Write one value to the config file, then reload it and (unless told otherwise) rescan."""
        try:
            path = config.save_setting(keys, value)
            self.cfg = config.load(self.root_override)
        except config.ConfigError as e:
            self.notify(str(e), severity="error")
            return False
        if not quiet:
            self.notify(f"Saved to {path}")
        if rescan:
            self.load_everything()
        return True

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        self.view = (event.tab.id or "view-all").removeprefix("view-")
        self.query_one("#repos").display = self.view not in TABLES
        for view, table_id in TABLES.items():
            self.query_one(table_id).display = self.view == view
        if not isinstance(self.focused, Input):
            self._table().focus()
        self.refresh_bindings()
        if self.view == "org":
            self._open_org()
        self._refresh()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Keep the footer to what works in the current view."""
        if self.view == "tools" and action in NOT_ON_TOOLS:
            return False
        if action in ("extras", "cycle_view") and len(self.screen_stack) > 1:
            return False  # a tool screen is open
        if action in REPO_ONLY_ACTIONS:
            return self.view not in TABLES
        if action == "sort":
            return self.view != "pack"
        if action == "clone":
            return self.view == "pack"
        if action == "checkout":
            return self.view in PR_VIEWS
        if action == "toggle_team":
            return self.view == "reviews"
        if action in ("toggle_drafts", "pr_filters"):
            return self.view == "org"
        if action == "rescan":
            return self.view != "org"
        if action == "reload_org":
            return self.view == "org"
        if action == "open_ide":
            return not self.cfg.ide_off and (self.view not in PR_VIEWS or self._selected_repo() is not None)
        return True

    def on_input_changed(self, event: Input.Changed) -> None:
        self.needle = event.value.strip().lower()
        self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            event.input.display = False
        self._table().focus()

    # --- actions --------------------------------------------------------------

    def _modal(self) -> bool:
        return len(self.screen_stack) > 1

    def action_cycle_view(self, step: int) -> None:
        if self._modal():
            return
        keys = list(VIEWS)
        self.query_one(Tabs).active = f"view-{keys[(keys.index(self.view) + step) % len(keys)]}"

    def _open_tool(self, key: str | None) -> None:
        if key == "spark" and not self._modal():
            from .tui_spark import SparkScreen  # imports this module, so not at the top

            self.push_screen(SparkScreen())

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

    def action_sort(self) -> None:
        if self._modal() or self.view == "pack":
            return
        if self.view in PR_VIEWS:
            keys = list(PR_SORTS)
            self.pr_sort = keys[(keys.index(self.pr_sort) + 1) % len(keys)]
        else:
            keys = list(SORTS)
            self.sort = keys[(keys.index(self.sort) + 1) % len(keys)]
        self._refresh()

    @work(group="reviews", exclusive=True)
    async def action_toggle_team(self) -> None:
        if self._modal() or self.view != "reviews":
            return
        self.team_reviews = not self.team_reviews
        if self.team_reviews:
            self.notify("Loading team review requests, this takes a few seconds...")
        await self._load_reviews()

    @work(group="dialog")
    async def action_pr_filters(self) -> None:
        """Toggle the All PRs filters; they work on the loaded list, so nothing is searched again."""
        if self._modal() or self.view != "org":
            return
        keys = dict(zip(github.PR_FILTERS, "lpus", strict=True))
        while True:
            wanted = set(self.cfg.pr_filters)
            actions = [
                (keys[f], f, f"[{'x' if f in wanted else ' '}] {label}", None) for f, label in github.PR_FILTERS.items()
            ]
            actions.append(("n", "none", "Show all", None if wanted else "nothing filtered"))
            choice = await self.push_screen_wait(MenuScreen(Text("All PRs: show only", "bold"), actions))
            if not choice:
                return
            wanted = set() if choice == "none" else wanted ^ {choice}
            if choice == "unreviewed" and self.org and any(i.pr.reviews is None for i in self.org.items):
                self.notify("Review counts come with the next search (r); until then no PR is left out for it")
            chosen = [f for f in github.PR_FILTERS if f in wanted]
            if not self._save_setting(("pr_filters",), chosen or None, rescan=False, quiet=True):
                return
            self._refresh()

    def action_toggle_drafts(self) -> None:
        if not self._modal() and self.view == "org":
            self.org_drafts = not self.org_drafts
            self._refresh()

    def action_reload_org(self) -> None:
        if not self._modal() and self.view == "org":
            self.reload_org()

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
        if st and not self.cfg.ide_off:
            self._open_ide(st)

    @work(group="ide")
    async def _open_ide(self, st: RepoState) -> None:
        ide = config.find_ide(self.cfg)
        if not ide or not config.runnable(ide):
            why = f"{ide} cannot be found." if ide else "No IntelliJ launcher found (Toolbox script or idea on PATH)."
            ide = await self._ask_ide(why)
            if not ide:
                return
        try:
            proc.launch(ide, str(st.path))
        except OSError as e:
            self.notify(f"Could not start {ide}: {e}. Extras (x) can change it.", severity="error")
            return
        self.notify(f"Opening {st.name}")

    async def _ask_ide(self, why: str = "") -> str | None:
        """Ask for the IntelliJ launcher and save it as ide_command. Returns it, or None when cancelled or cleared.

        "off" saves ide_command = false, which hides the IDE actions for people who do not use IntelliJ.
        """
        hint = (
            f"{why} Path to idea64.exe or an idea script, or a command on PATH."
            " Empty: find it automatically. off: no IDE actions."
        )
        current = "off" if self.cfg.ide_off else self.cfg.ide_command or ""
        answer = await self.push_screen_wait(PromptScreen("IntelliJ launcher", current, hint.strip()))
        if answer is None:
            return None
        off = answer.lower() == "off"
        if answer and not off and not config.runnable(answer):
            self.notify(f"Not found: {answer}. Type off if you do not use IntelliJ.", severity="error")
            return None
        try:
            path = config.save_setting(("ide_command",), False if off else answer or None)
        except config.ConfigError as e:
            self.notify(str(e), severity="error")
            return None
        self.cfg.ide_off, self.cfg.ide_command = off, None if off else answer or None
        self.refresh_bindings()
        if off:
            self.notify("IDE actions turned off. Extras (x) turns them back on.")
            return None
        self.notify(f"Saved to {path}")
        return answer or None

    @work(group="dialog")
    async def action_extras(self) -> None:
        if self._modal():
            return
        choice = await self.push_screen_wait(ExtrasMenu(self.cfg, self.pack_info))
        if choice == "refresh":
            self.notify("Refreshing pack data...")
            await self._load_pack(force=True)
            self.notify("Pack data refreshed")
        elif choice in ("config", "logs"):
            self.action_open_folder(choice)
        elif choice == "purge_jars":
            await self._purge_jars()
        elif choice == "jars":
            folder = self.cfg.root / pack.JAR_FOLDER
            self.notify("Downloading closed-source jars...")
            report = await pack.download_closed(self.entries, folder)
            for line in report.failed:
                self.notify(line, severity="error")
            self.notify(f"{report.summary} in {folder}")
        elif choice == "unhide":
            # Number keys pick a repo; beyond nine, use the arrows.
            names = self.cfg.hidden
            actions = [(str(i) if i < 10 else " ", name, name, None) for i, name in enumerate(names, 1)]
            name = await self.push_screen_wait(MenuScreen(Text("Show which repo again?", "bold"), actions))
            if name:
                self._save_setting(("repos", name, "ignore"), None)
        elif choice == "unkeep":
            kept = self.cfg.kept
            actions = [
                (str(i) if i < 10 else " ", str(i), f"{repo}  {br}", None) for i, (repo, br) in enumerate(kept, 1)
            ]
            picked = await self.push_screen_wait(MenuScreen(Text("Unprotect which branch?", "bold"), actions))
            if picked:
                repo, br = kept[int(picked) - 1]
                rest = [b for b in self.cfg.repo(repo).keep_branches if b != br]
                self._save_setting(("repos", repo, "keep_branches"), rest or None)
        elif choice == "ide":
            await self._ask_ide()
        elif choice == "protocol":
            new = "https" if self.cfg.clone_protocol == "ssh" else "ssh"
            if self._save_setting(("clone_protocol",), new, rescan=False, quiet=True):
                self.notify(f"New clones and remotes use {new.upper()}; existing ones are left as they are")
        elif choice == "clone_many":
            await self._clone_many_menu()
        elif choice == "refresh_fetch":
            on = not self.cfg.refresh_fetch
            if self._save_setting(("refresh_fetch",), on or None, rescan=False, quiet=True):
                self.notify("Auto-refresh now fetches every repo" if on else "Auto-refresh now only rescans")
        elif choice == "auto":
            hint = "Minutes between automatic rescans, 0 to turn them off."
            value = await self.push_screen_wait(
                PromptScreen("Auto-refresh interval", str(self.cfg.refresh_minutes), hint)
            )
            if value is None:
                return
            if not value.isdigit():
                self.notify(f"Not a number of minutes: {value}", severity="error")
                return
            self._save_setting(("refresh_minutes",), int(value))
        elif choice == "root":
            await self._ask_root(str(self.cfg.root))

    async def _purge_jars(self) -> None:
        found = ops.built_jars(self.states.values(), self.cfg.org)
        if not found:
            self.notify(f"No built jars in build/libs of {self.cfg.org} repos")
            return
        files = [p for _, jars in found for p in jars]
        total = sum(ops.file_size(p) for p in files)
        lines = [
            Text.assemble((st.name, "bold"), f"  {len(jars)} jars, {render.megabytes(sum(map(ops.file_size, jars)))}")
            for st, jars in found
        ]
        lines.append(Text("\nDeleted for good, not to the recycle bin. The next build makes them again.", "dim"))
        title = f"Delete {len(files)} built jars ({render.megabytes(total)}) in {len(found)} repos?"
        if not await self.push_screen_wait(ConfirmScreen(title, Text("\n").join(lines))):
            return
        freed, failed = await asyncio.to_thread(ops.delete_files, files)
        for line in failed[:5]:
            self.notify(line, severity="error", timeout=10)
        more = f", {len(failed)} could not be deleted (in use?)" if failed else ""
        self.notify(f"Freed {render.megabytes(freed)}{more}", severity="warning" if failed else "information")

    async def _clone_many_menu(self) -> None:
        protocol = self.cfg.clone_protocol
        pack_jobs = pack.missing_jobs(self.entries, self.cfg.root, protocol)
        org = self.cfg.org
        actions = [
            ("p", "pack", f"Pack mods not checked out ({len(pack_jobs)})", None if pack_jobs else "none missing"),
            ("o", "org", f"{org} repos not checked out, archived ones left out (asks GitHub)", None),
        ]
        if not self.entries:
            actions[0] = (*actions[0][:3], "pack data not loaded")
        info = Text(f"Into {self.cfg.root} over {protocol.upper()}. Folders that already exist are skipped.", "dim")
        choice = await self.push_screen_wait(MenuScreen(Text("Clone missing repos", "bold"), actions, info))
        if not choice:
            return
        if choice == "pack":
            jobs, what = pack_jobs, "pack mods"
        else:
            self.notify(f"Listing the repos of {org}...")
            repos, error = await github.org_repos(org)
            if error:
                self.notify(f"{org} repos: {error}", severity="error")
                if not repos:
                    return
            repos = [r for r in repos if not r.archived]
            jobs, what = ops.org_jobs(repos, self.states.values(), self.cfg.root, protocol), f"{org} repos"
        if not jobs:
            self.notify(f"Every one of the {what} is checked out already")
            return
        body = Text("\n".join(j.name for j in jobs))
        if await self.push_screen_wait(ConfirmScreen(f"Clone {len(jobs)} {what}?", body)):
            self._clone_many(jobs)

    @work(group="clone")
    async def _clone_many(self, jobs: list[CloneJob]) -> None:
        if self.busy:
            self.notify(f"{self.busy} is still running", severity="warning")
            return
        self.busy = "Cloning"
        done = 0

        def on_done(job: CloneJob, why: str | None) -> None:
            nonlocal done
            done += 1
            self.sub_title = f"Cloning {done}/{len(jobs)}"

        try:
            failed = await ops.clone_all(jobs, min(4, self.cfg.concurrency), on_done)
        finally:
            self.busy = ""
        for job, why in failed[:5]:
            self.notify(f"{job.name}: {why}", severity="error", timeout=10)
        cloned = [j.dest for j in jobs if j.dest.is_dir() and j not in {f for f, _ in failed}]
        if cloned:
            await self._batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), cloned)
        more = f", {len(failed)} failed" if failed else ""
        self.notify(f"Cloned {len(cloned)} repos{more}", severity="warning" if failed else "information")

    @work(group="dialog")
    async def _first_root(self) -> None:
        """The folder with the checkouts does not exist (first start on another setup): ask for it."""
        guess = config.suggest_root()
        why = f'"{self.cfg.root}" does not exist.'
        if not await self._ask_root(str(guess or self.cfg.root), why):
            self.notify("No repo folder yet: Extras (x) → Change the repo folder", severity="warning", timeout=10)

    async def _ask_root(self, current: str, why: str = "") -> bool:
        """Ask for the folder with the checkouts until it exists, then save it. False when cancelled."""
        hint = f"{why} Folder that holds your checkouts (one repo per subfolder).".strip()
        while True:
            folder = await self.push_screen_wait(PromptScreen("Repo folder", current, hint))
            if not folder:
                return False
            if Path(folder).expanduser().is_dir():
                break
            self.notify(f"Not a folder: {folder}", severity="error")
            current = folder
        self.root_override = None
        self._save_setting(("root",), folder)
        return True

    def action_open_folder(self, which: str) -> None:
        folder = config.config_path().parent if which == "config" else config.cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        proc.open_path(folder)

    def action_open_web(self) -> None:
        review = self._selected_review()
        if review:
            webbrowser.open(review.pr.url)
        else:
            self._open_web(self._selected_repo(), self._selected_entry())

    def _open_web(self, st: RepoState | None, entry: PackEntry | None) -> None:
        url = None
        if st:
            pr = st.current.pr if st.current else None
            url = pr.url if pr else (f"https://github.com/{st.slug}" if st.slug else None)
        elif entry:
            url = entry.web_url
        if url:
            webbrowser.open(url)

    @work(group="dialog")
    async def action_cleanup(self) -> None:
        if not self._modal():
            await self._cleanup(list(self.states.values()))

    async def _cleanup(self, states: list[RepoState]) -> None:
        if self.busy:
            return
        safe, unpushed = await ops.cleanup_candidates(states, parked=True)
        if not safe:
            kept = f" ({len(unpushed)} merged ones kept: local commits not in their PR)" if unpushed else ""
            self.notify(f"No merged branches to clean up{kept}")
            return
        parked = {st.path: st for st, br in safe if br.name == st.head}  # checked out: needs a switch to main
        body = Text("\n").join(
            Text.assemble(
                (st.name, "bold"),
                f"  {br.name}",
                (f"  PR #{br.pr.number}", "dim"),
                (f"  checked out, switches to {st.tracked} first" if st.path in parked else "", "yellow"),
            )
            for st, br in safe
        )
        for st, remote in ops.orphaned_remotes(safe):
            body.append("\n").append_text(
                Text.assemble((st.name, "bold"), f"  remote {remote.name}", ("  no longer tracked", "dim"))
            )
        option = f"Switch {len(parked)} repos to main first (unticked: keep their branch)" if parked else None
        screen = ConfirmScreen(f"Delete {len(safe)} merged local branches?", body, option, self.cfg.cleanup_to_main)
        if not await self.push_screen_wait(screen):
            return
        if parked:
            if screen.checked != self.cfg.cleanup_to_main:
                self.cfg.cleanup_to_main = screen.checked
                try:
                    config.save_setting(("cleanup_to_main",), screen.checked)
                except config.ConfigError as e:
                    self.notify(str(e), severity="error")
            if screen.checked:
                await self._batch("Switching", lambda p: ops.to_main(self.cfg, parked[p]), list(parked))
            # Whatever is still checked out (unticked, or the switch failed) stays.
            safe = [(st, br) for st, br in safe if self.states.get(str(st.path), st).head != br.name]
            if not safe:
                self.notify("Nothing deleted: the merged branches are still checked out")
                return
        report = await ops.cleanup(safe)
        log_file = config.cache_dir() / "deleted-branches.log"
        if report.restore:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as f:
                f.writelines(f"{datetime.now():%Y-%m-%d %H:%M} {line}\n" for line in report.restore)
        await self._batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), list({st.path for st, _ in safe}))
        note = f"Deleted {report.branches} branches and {report.remotes} remotes, undo commands in {log_file}"
        failed = len(report.failed)
        self.notify(note + (f"; {failed} failed" if failed else ""), severity="warning" if failed else "information")

    @work(group="dialog")
    async def action_to_main(self) -> None:
        if self._modal() or self.busy:
            return
        candidates = {st.path: st for st in ops.to_main_candidates(self.states.values())}
        if not candidates:
            self.notify("No repo is parked on a merged branch with a clean working tree")
            return
        body = Text("\n").join(
            Text.assemble((st.name, "bold"), f"  {st.head} → {st.tracked}", (f"  PR #{st.current.pr.number}", "dim"))
            for st in candidates.values()
        )
        if not await self.push_screen_wait(ConfirmScreen(f"Switch {len(candidates)} repos back to main?", body)):
            return
        if await self._batch("Switching", lambda p: ops.to_main(self.cfg, candidates[p]), list(candidates)):
            self.notify("Switched. Press d to delete the merged branches.")

    @work(group="checkout")
    async def action_checkout(self) -> None:
        item, st = self._selected_review(), self._selected_repo()
        if self._modal() or not item:
            return
        if not st:
            self.notify(f"{item.repo} is not checked out here", severity="warning")
            return
        if st.dirty:
            self.notify(f"{st.name} has uncommitted changes", severity="error")
            return
        self.notify(f"Checking out #{item.pr.number} in {st.name}...")
        head, error = await github.pr_head(item.repo, item.pr.number, self.cfg.clone_protocol)
        if not head:
            self.notify(f"#{item.pr.number}: {error}", severity="error")
            return
        if not await self._batch("Checking out", lambda p: ops.checkout_pr(self.cfg, st, head), [st.path]):
            return
        new = self.states.get(str(st.path))
        if new and new.note:
            if new.note.kind != "failed" and new.head:
                # Link the PR right away instead of waiting for the next PR reload.
                self.prs.prs[(new.name, new.head)] = item.pr
                github.attach([new], self.prs)
                self._refresh()
            self.notify(f"{st.name}: {new.note.text}", severity="error" if new.note.kind == "failed" else "information")

    @work(group="clone")
    async def action_clone(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self.notify("Clone works from the Pack view")
            return
        if entry.local:
            return
        self.notify(f"Cloning {entry.repo or entry.name}...")
        res = await pack.clone(entry, self.cfg.root, self.cfg.clone_protocol)
        if not res.ok:
            self.notify(f"{entry.name}: {res.message}", severity="error")
            return
        await self._batch(
            "Scanning", lambda p: ops.scan_repo(self.cfg, p), [self.cfg.root / entry.repo.partition("/")[2]]
        )
        self.notify(f"Cloned {entry.repo}")
