"""Terminal UI on Textual: the repo, PR, pack and tools tabs, their keys, menus and dialogs.

What the tabs show comes from views.py, the data and the work on it from workbench.py; both are shared with the
desktop UI. `nh-workbench web` serves this same UI in a browser tab.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from . import config, github, keys, ops, pack, proc, render, skill
from .config import Config
from .models import CloneJob, PackEntry, PackInfo, RepoState, ReviewItem
from .views import (
    CHECKOUT_ACTIONS,
    HEADER_SORT,
    PACK_COLUMNS,
    PR_HEADER_SORT,
    PR_SORTS,
    PR_VIEWS,
    REPO_COLUMNS,
    SORTS,
    TAB_GROUP_STARTS,
    TABLES,
    TOOL_COLUMNS,
    TOOLS,
    VIEWS,
    action_shown,
    cleanup_body,
    local_branch,
    mine_detail,
    no_cleanup_note,
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
    to_main_body,
    tool_behind,
    tool_entry,
    tool_status,
)
from .workbench import Workbench


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
    def __init__(self, cfg: Config, info: PackInfo | None, update: tuple[str, str | None]) -> None:
        lines = [Text(f"Root: {cfg.root}", "dim"), Text(f"Config: {config.config_path()}", "dim")]
        if info:
            lines.append(render.pack_header(info))
        every = f"every {cfg.refresh_minutes} min" if cfg.refresh_minutes else "off"
        ide_now = "off" if cfg.ide_off else cfg.ide_command or "found automatically"
        other = "HTTPS" if cfg.clone_protocol == "ssh" else "SSH"
        actions = [
            ("a", "auto", f"Auto-refresh interval (now {every})", None),
            ("f", "refresh_fetch", f"Fetch during auto-refresh (now {'on' if cfg.refresh_fetch else 'off'})", None),
            ("i", "ide", f"IntelliJ launcher (now {ide_now})", None),
            ("s", "protocol", f"Clone over {other} (now {cfg.clone_protocol.upper()})", None),
            ("r", "refresh", "Refresh pack data now (otherwise hourly)", None),
            ("o", "root", "Change the repo folder", None),
            ("u", "unhide", f"Show a hidden repo again ({len(cfg.hidden)})", None if cfg.hidden else "none hidden"),
            ("k", "unkeep", f"Unprotect a kept branch ({len(cfg.kept)})", None if cfg.kept else "none protected"),
            ("b", "keys", "Keys: change the main keys (shared with the desktop UI)", None),
            ("c", "config", "Open config folder", None),
            ("l", "logs", "Open logs and cache folder", None),
            ("n", "update", *update),
        ]
        lines.append(Text("Clone missing repos, closed-source jars, built jars and the AI skill: Tools tab", "dim"))
        super().__init__(Text("Settings", "bold"), actions, Group(*lines))


class KeysScreen(ModalScreen[None]):
    """The main keys: Enter on one, then press its new key. Saved at once, shared with the desktop UI."""

    BINDINGS = [Binding("escape,q,backspace,left", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self.waiting: str | None = None  # the action whose new key is awaited

    def _prompt(self, action: keys.KeyAction) -> Text:
        letter = keys.letters(self.app.cfg)[action.id]
        default = Text(f"  (default {action.letter})", "dim") if letter != action.letter else ""
        return Text.assemble((letter.ljust(3), "bold"), action.label, default)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield dialog_head(Text("Keys", "bold"))
            yield Static(Text("Shared with the desktop UI, where they work on the list.", "dim"), id="dialog-info")
            yield OptionList(*(Option(self._prompt(a), id=a.id) for a in keys.ACTIONS), id="menu")
            yield Static(Text("Enter to change one, q or Esc to close", "dim"), id="keys-status")

    def _status(self, text: str, style: str = "dim") -> None:
        self.query_one("#keys-status", Static).update(Text(text, style))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.waiting = event.option.id
        action = keys.BY_ID[self.waiting]
        self._status(f"Press the new key for {action.label} (Esc: keep it, Backspace: default {action.letter})", "bold")

    def on_key(self, event: Key) -> None:
        if self.waiting is None:
            return
        event.stop()
        event.prevent_default()
        action = keys.BY_ID[self.waiting]
        if event.key == "escape":
            letter = None
        elif event.key == "backspace":
            letter = action.letter
        elif event.character and not keys.letter_error(event.character):
            letter = event.character
        else:
            self._status(f"{event.key} types no character: pick a letter, digit or sign", "red")
            return
        if letter is not None:
            why = self.app.set_letter(action.id, letter)
            if why:
                self._status(f"{why}: press another key for {action.label}, or Esc to keep it", "red")
                return
            self.query_one(OptionList).replace_option_prompt(action.id, self._prompt(action))
        self.waiting = None
        self._status("Enter to change one, q or Esc to close")

    def action_close(self) -> None:
        self.dismiss(None)


class RepoApp(App[None]):
    CSS_PATH = "tui.tcss"
    TITLE = "NH Workbench"
    # The ids are keys.ACTIONS: on_mount sets their keys from the config.
    BINDINGS = [
        Binding("f", "fetch", "Fetch all", id="fetch"),
        Binding("p", "pull", "Pull safe", id="pull"),
        Binding("F", "fetch(True)", "Fetch one", show=False, id="fetch_one"),
        Binding("P", "pull(True)", "Pull one", show=False, id="pull_one"),
        Binding("r", "rescan", "Rescan", id="rescan"),
        Binding("r", "reload_org", "Search again", id="reload_org"),
        Binding("r", "reload_prs", "Refresh from GitHub", id="reload_prs"),
        Binding("slash", "filter", "Filter", id="filter"),
        Binding("o", "open_ide", "IDE", id="open_ide"),
        Binding("e", "open_files", "Files", show=False, id="open_files"),
        Binding("T", "open_terminal", "Terminal", show=False, id="open_terminal"),
        Binding("w", "open_web", "Web", id="open_web"),
        Binding("m", "to_main", "To main", id="to_main"),
        Binding("d", "cleanup", "Cleanup", id="cleanup"),
        Binding("c", "clone", "Clone", id="clone"),
        Binding("c", "checkout", "Check out", id="checkout"),
        Binding("s", "sort", "Sort", id="sort"),
        Binding("t", "toggle_team", "Team reviews", id="toggle_team"),
        Binding("t", "toggle_drafts", "Drafts", id="toggle_drafts"),
        Binding("v", "pr_filters", "Show only", id="pr_filters"),
        Binding("x", "extras", "Settings", id="extras"),
        Binding("C", "open_folder('config')", "Config dir", show=False, id="open_config"),
        Binding("L", "open_folder('logs')", "Logs dir", show=False, id="open_logs"),
        Binding("tab", "cycle_view(1)", "Next view", priority=True),
        Binding("shift+tab", "cycle_view(-1)", show=False, priority=True),
        Binding("escape", "clear_filter", show=False),
        Binding("q", "quit", "Quit", id="quit"),
    ]

    def __init__(self, cfg: Config, root_override: Path | None = None) -> None:
        super().__init__()
        self.wb = Workbench(
            cfg,
            on_repo=self._update_row,
            on_change=self._on_change,
            on_progress=self._on_progress,
            notify=lambda text, severity: self.notify(text, severity=severity),
            name_of=lambda action: f"{keys.BY_ID[action].label} ({keys.hint(self.cfg, action)})",
        )
        self.root_override = root_override  # --root on the command line wins over the config file
        self.sort = "name"
        self.pr_sort = "updated"
        self.org_drafts = False
        self.last_loaded = time.monotonic()
        self.view = "all"
        self.needle = ""
        self.pack_labelled: PackInfo | None = None  # the pack data the Pack tab's headers name

    # The data lives in the Workbench, which the desktop UI uses too.
    @property
    def cfg(self) -> Config:
        return self.wb.cfg

    @cfg.setter
    def cfg(self, cfg: Config) -> None:
        self.wb.cfg = cfg

    @property
    def states(self) -> dict[str, RepoState]:
        return self.wb.states

    @property
    def pack_info(self) -> PackInfo | None:
        return self.wb.pack_info

    @property
    def entries(self) -> list[PackEntry]:
        return self.wb.entries

    @property
    def mine(self) -> list[ReviewItem]:
        return self.wb.mine

    @property
    def reviews(self) -> list[ReviewItem]:
        return self.wb.reviews

    @property
    def org(self) -> github.OrgPrs | None:
        return self.wb.org  # loaded the first time the All PRs tab opens

    @property
    def busy(self) -> str:
        return self.wb.busy

    @property
    def tool_updated(self) -> bool:
        return self.wb.tool_updated

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tabs(
            *(
                Tab(label, id=f"view-{key}", classes="group-start" if key in TAB_GROUP_STARTS else "")
                for key, (label, _) in VIEWS.items()
            )
        )
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
        self._apply_keys()
        self._check_tool()
        if self.cfg.root.is_dir():
            self.load_everything()
        else:
            self._first_root()
        # Checked every minute, so a new interval set from Settings applies without a restart.
        self.set_interval(60, self.auto_refresh)

    # --- updating the tool itself -------------------------------------------------

    @work(group="tool-update", exclusive=True)
    async def _check_tool(self) -> None:
        await self.wb.check_tool()

    @property
    def _settings(self) -> str:
        return f"Settings ({keys.hint(self.cfg, 'extras')})"

    def _tool_behind(self) -> int:
        return tool_behind(self.wb.tool)

    def _tool_entry(self) -> tuple[str, str | None]:
        return tool_entry(self.wb.tool, self.wb.tool_updated)

    async def _update_tool(self) -> None:
        await self.wb.update_tool()

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
        await self.wb.load(fetch)

    @work(group="org")
    async def _open_org(self) -> None:
        """Show the saved org search at once; search again only when it is missing or old."""
        await self.wb.open_org()

    @work(group="org")
    async def reload_org(self) -> None:
        await self.wb.reload_org()

    def _org_shown(self) -> list[ReviewItem]:
        items = self.org.items if self.org else []
        return org_shown(items, self.org_drafts, self.cfg.pr_filters, self.states.values(), self.entries)

    def _org_mark(self, item: ReviewItem) -> str:
        return org_mark(item, self.mine, self.reviews, self.org.reviewed if self.org else set())

    def _on_change(self, *_lists) -> None:
        if self.pack_info is not self.pack_labelled:  # rebuild the headers so they name the daily and the release
            self.pack_labelled = self.pack_info
            table = self.query_one("#pack", DataTable)
            labels = render.pack_labels(self.pack_info)
            table.clear(columns=True)
            for column in PACK_COLUMNS:
                table.add_column(labels.get(column, column), key=column)
        self._refresh()

    def _on_progress(self, label: str, done: int, total: int) -> None:
        if label:
            self.sub_title = f"{label} {done}/{total}"
        else:  # the job is over: back to the summary
            self._refresh()

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
        return local_branch(item, self.states.values())

    def _repo_for(self, item: ReviewItem) -> RepoState | None:
        return repo_for(item, self.states.values())

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
        shown = [i for i in items if pr_matches(i, self.needle)]
        key = PR_SORTS[self.pr_sort][1]
        return sorted(shown, key=key) if key else shown

    def _update_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        if self.view == "tools":
            key = self._cursor_key()
            tool = next((t for t in TOOLS if t[0] == key), None)
            if tool:
                detail.update(Group(Text(tool[3]), Text(self._tool_status(tool[0]), "bold")))
            else:
                detail.update(Text("nothing selected", "dim"))
            return
        k = keys.letters(self.cfg)
        entry, st, review = self._selected_entry(), self._selected_repo(), self._selected_review()
        if review and self.view == "mine":
            detail.update(mine_detail(review, self._local_branch(review), k))
        elif review and self.view == "org":
            detail.update(org_detail(review, self._org_mark(review), st, k))
        elif review:
            detail.update(review_detail(review, st, k))
        elif entry and self.pack_info:
            detail.update(pack_detail(entry, self.pack_info, k))
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
            await self.wb.batch("Fetching", lambda p: ops.fetch_repo(self.cfg, p), one)
        elif choice == "pull":
            await self.wb.batch("Pulling", lambda p: ops.pull_repo(self.cfg, p), one)
        elif choice == "main":
            await self.wb.batch("Switching", lambda p: ops.to_main(self.cfg, st), one)
        elif choice == "cleanup":
            await self._cleanup([st])
        elif choice == "ide":
            self._open_ide(st)
        elif choice == "web":
            self._open_web(st, None)
        elif choice == "folder":
            proc.open_path(st.path)
        elif choice == "terminal":
            self._open_terminal(st)
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
            body = Text(f"Adds ignore = true for {st.name} to the config file.\nTo undo, edit the file (Settings, c).")
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
        if action in ("extras", "cycle_view") and len(self.screen_stack) > 1:
            return False  # a tool screen is open
        selected = action not in CHECKOUT_ACTIONS or self._selected_repo() is not None  # looked up only when needed
        return action_shown(action, self.view, self.cfg.ide_off, selected)

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
        if self._modal():
            return
        if key == "spark":
            from .tui_spark import SparkScreen  # imports this module, so not at the top

            self.push_screen(SparkScreen())
        elif key:
            self._run_tool(key)

    @work(group="dialog")
    async def _run_tool(self, key: str) -> None:
        if key == "clone":
            await self._clone_many_menu()
        elif key == "jars":
            await self.wb.download_jars()
        elif key == "clean":
            await self._purge_jars()
        elif key == "skill":
            await self.install_skill_menu()

    def _tool_status(self, key: str) -> str:
        return tool_status(key, self.cfg, self.entries, self.states.values())

    async def install_skill_menu(self) -> None:
        """Install the skill that lets Claude Code, Codex or OpenCode read spark links with this tool."""
        actions = []
        for key, (agent, label) in zip("123", skill.AGENTS.items(), strict=True):
            state = "update" if skill.installed(agent) else "install"
            actions.append((key, agent, f"{label}: {state} in {skill.skills_dir(agent) / skill.SKILL}", None))
        actions.append(("a", "all", "All", None))
        info = Text("Spark links pasted in a chat with that agent then get read with this tool, in any project.", "dim")
        choice = await self.push_screen_wait(MenuScreen(Text("Install the spark-profile skill", "bold"), actions, info))
        if not choice:
            return
        await self.wb.install_skill(list(skill.AGENTS) if choice == "all" else [choice])
        if self.view == "tools":
            self._update_detail()

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
        on = not self.wb.team_reviews
        if on:
            self.notify("Loading team review requests, this takes a few seconds...")
        await self.wb.toggle_team(on)

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
                self.notify(
                    f"Review counts come with the next search ({keys.hint(self.cfg, 'reload_org')}); "
                    "until then no PR is left out for it"
                )
            chosen = [f for f in github.PR_FILTERS if f in wanted]
            if not self._save_setting(("pr_filters",), chosen or None, rescan=False, quiet=True):
                return
            self._refresh()

    def action_toggle_drafts(self) -> None:
        if not self._modal() and self.view == "org":
            self.org_drafts = not self.org_drafts
            self._refresh()

    @work(group="prs", exclusive=True)
    async def action_reload_prs(self) -> None:
        """Load your PRs, review requests and branch PRs again, without rescanning the repos."""
        if self._modal() or self.view not in ("mine", "reviews"):
            return
        self.notify("Refreshing from GitHub...")
        await self.wb.reload_prs()

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
        await self.wb.batch(label, worker, paths)

    def action_open_files(self) -> None:
        st = self._selected_repo()
        if st:
            proc.open_path(st.path)

    def action_open_terminal(self) -> None:
        st = self._selected_repo()
        if st:
            self._open_terminal(st)

    def _open_terminal(self, st: RepoState) -> None:
        try:
            proc.open_terminal(st.path)
        except OSError as e:
            self.notify(f"Could not open a terminal: {e}", severity="error")

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
            self.notify(f"Could not start {ide}: {e}. {self._settings} can change it.", severity="error")
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
            self.notify(f"IDE actions turned off. {self._settings} turns them back on.")
            return None
        self.notify(f"Saved to {path}")
        return answer or None

    @work(group="dialog")
    async def action_extras(self) -> None:
        if self._modal():
            return
        choice = await self.push_screen_wait(ExtrasMenu(self.cfg, self.pack_info, self._tool_entry()))
        if choice == "refresh":
            self.notify("Refreshing pack data...")
            await self.wb.refresh_pack()
            self.notify("Pack data refreshed")
        elif choice in ("config", "logs"):
            self.action_open_folder(choice)
        elif choice == "update":
            await self._update_tool()
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
        elif choice == "keys":
            await self.push_screen_wait(KeysScreen())

    def _apply_keys(self) -> None:
        letters = keys.letters(self.cfg)
        self.set_keymap(letters)
        for a, b in keys.clashes(letters):  # edited by hand in the config file: the Keys screen does not let them
            label = f"{letters[a]} is both {keys.BY_ID[a].label} and {keys.BY_ID[b].label}"
            self.notify(f"{label}: Settings → Keys", severity="warning", timeout=15)

    def handle_bindings_clash(self, clashed_bindings, node) -> None:
        pass  # keys shared on purpose by actions never on the same tab (r, c, t); real clashes are told on start

    def set_letter(self, action_id: str, letter: str) -> str | None:
        """Give an action a new key and save it. The reason it cannot be, or None."""
        mapping = {**keys.letters(self.cfg), action_id: letter}
        why = keys.clash_text(mapping, action_id)
        if why:
            return why
        value = None if letter == keys.BY_ID[action_id].letter else letter
        if not self._save_setting(("keys", action_id), value, rescan=False, quiet=True):
            return "Could not save it"
        self.set_keymap(keys.letters(self.cfg))
        self._update_detail()
        return None

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
        await self.wb.delete_jars(files)

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
            found = await self.wb.org_jobs()
            if found is None:
                return
            jobs, what = found, f"{org} repos"
        if not jobs:
            self.notify(f"Every one of the {what} is checked out already")
            return
        body = Text("\n".join(j.name for j in jobs))
        if await self.push_screen_wait(ConfirmScreen(f"Clone {len(jobs)} {what}?", body)):
            self._clone_many(jobs)

    @work(group="clone")
    async def _clone_many(self, jobs: list[CloneJob]) -> None:
        await self.wb.clone_many(jobs)

    @work(group="dialog")
    async def _first_root(self) -> None:
        """The folder with the checkouts does not exist (first start on another setup): ask for it."""
        guess = config.suggest_root()
        why = f'"{self.cfg.root}" does not exist.'
        if not await self._ask_root(str(guess or self.cfg.root), why):
            self.notify(
                f"No repo folder yet: {self._settings} → Change the repo folder", severity="warning", timeout=10
            )

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
            self.open_url(review.pr.url)  # also works when the UI is shown in a browser tab
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
            self.open_url(url)

    @work(group="dialog")
    async def action_cleanup(self) -> None:
        if not self._modal():
            await self._cleanup(list(self.states.values()))

    async def _cleanup(self, states: list[RepoState]) -> None:
        if self.busy:
            return
        safe, unpushed = await self.wb.find_cleanup(states)
        if not safe:
            self.notify(no_cleanup_note(unpushed))
            return
        parked = {st.path: st for st, br in safe if br.name == st.head}  # checked out: needs a switch to main
        body = cleanup_body(safe, parked)
        option = f"Switch {len(parked)} repos to main first (unticked: keep their branch)" if parked else None
        screen = ConfirmScreen(f"Delete {len(safe)} merged local branches?", body, option, self.cfg.cleanup_to_main)
        if not await self.push_screen_wait(screen):
            return
        if parked and screen.checked != self.cfg.cleanup_to_main:
            self.cfg.cleanup_to_main = screen.checked
            try:
                config.save_setting(("cleanup_to_main",), screen.checked)
            except config.ConfigError as e:
                self.notify(str(e), severity="error")
        await self.wb.cleanup(safe, parked, screen.checked)

    @work(group="dialog")
    async def action_to_main(self) -> None:
        if self._modal() or self.busy:
            return
        candidates = {st.path: st for st in ops.to_main_candidates(self.states.values())}
        if not candidates:
            self.notify("No repo is parked on a merged branch with a clean working tree")
            return
        body = to_main_body(candidates.values())
        if not await self.push_screen_wait(ConfirmScreen(f"Switch {len(candidates)} repos back to main?", body)):
            return
        await self.wb.to_main_many(candidates)

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
        await self.wb.checkout(item, st)

    @work(group="clone")
    async def action_clone(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self.notify("Clone works from the Pack view")
            return
        if entry.local:
            return
        self.notify(f"Cloning {entry.repo or entry.name}...")
        await self.wb.clone(entry)
