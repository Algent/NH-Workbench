"""What both UIs show, without Textual or Qt: the tabs, columns and sorts, which actions each tab has, the detail
pane and dialog texts as Rich text, and the tool's own update.

The terminal UI shows the Rich text as is; the desktop UI turns it into HTML, so both show the same thing.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from . import config, git, github, ops, pack, render, skill
from .config import Config
from .models import Branch, PackEntry, PackInfo, RepoState, ReviewItem

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
# Where a group of tabs starts (repos, then pull requests, then the rest): both UIs draw a divider before it.
TAB_GROUP_STARTS = ("mine", "pack")
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
        " shows what it calls, c who calls it, y copies the report for an LLM. Same as `nh-workbench tools spark`.",
    ),
    (
        "clone",
        "Clone missing repos",
        "Clone the pack mods, or the org repos, you do not have yet",
        "Lists what is missing first and asks before cloning, a few at a time; folders that already exist are"
        " skipped. Uses SSH or HTTPS as chosen in Settings.",
    ),
    (
        "jars",
        "Closed-source jars",
        "Download the jars of closed-source pack mods, where DreamAssemblerXXL expects them",
        "Into _GTNH_Closed_Source in the repo folder; jars already there are left alone. Same as `nh-workbench jars`.",
    ),
    (
        "clean",
        "Clean built jars",
        "Delete the jars Gradle left in build/libs of the org's repos",
        "Only *.jar files directly in build/libs, of checkouts with a remote in the org; lists them with their"
        " size and asks first. They are deleted for good, the next build makes them again.",
    ),
    (
        "skill",
        "Install AI skill",
        "Let Claude Code, Codex or OpenCode read spark links with this tool, in any project",
        "Installs the spark-profile skill for the agent you pick. Same as `nh-workbench tools install-skill`.",
    ),
]
NOT_ON_TOOLS = {"sort", "open_ide", "open_files", "open_terminal", "open_web", "filter", "clone"}
CHECKOUT_ACTIONS = {"open_ide", "open_files", "open_terminal"}  # need the selected line checked out here
BACKPORTS_SHOWN = 4  # release-branch commit subjects listed in the detail pane


def pr_matches(item: ReviewItem, needle: str) -> bool:
    """The PR filter: a name, title or author, or a size like <200 for PRs under 200 changed lines."""
    if needle.startswith("<") and needle[1:].strip().isdigit():
        return item.pr.size < int(needle[1:])
    return needle in f"{item.repo} {item.pr.title} {item.author}".lower()


def local_branch(item: ReviewItem, states: Iterable[RepoState]) -> tuple[RepoState, str] | None:
    """The checkout and local branch a pull request was opened from, when it is here."""
    for st in states:
        for br in st.branches:
            if br.pr and br.pr.url == item.pr.url:
                return st, br.name
    return None


def repo_for(item: ReviewItem, states: Iterable[RepoState]) -> RepoState | None:
    """The checkout a pull request belongs to: the one with its branch, or any with its repo as a remote."""
    states = list(states)
    local = local_branch(item, states)
    if local:
        return local[0]
    wanted = item.repo.lower()
    # Any remote counts: a PR to an upstream repo still belongs to the fork checked out here.
    for st in sorted(states, key=lambda s: s.worktree_of is not None):
        if any((r.slug or "").lower() == wanted for r in st.remotes.values()) or (st.slug or "").lower() == wanted:
            return st
    return None


def org_mark(item: ReviewItem, mine: Iterable[ReviewItem], reviews: Iterable[ReviewItem], reviewed: set[str]) -> str:
    """How you are involved in an org PR: author, requested (your review), reviewed, or not at all."""
    url = item.pr.url
    if any(i.pr.url == url for i in mine):
        return "author"
    if any(i.pr.url == url for i in reviews):
        return "requested"
    return "reviewed" if url in reviewed else ""


def org_shown(
    items: Iterable[ReviewItem],
    drafts: bool,
    filters: Iterable[str],
    states: Iterable[RepoState],
    entries: Iterable[PackEntry],
) -> list[ReviewItem]:
    """The org PRs the All PRs tab lists: drafts only when asked, then the "show only" filters."""
    items = [i for i in items if drafts or not i.pr.draft]
    filters = list(filters)
    if not filters:
        return items
    return github.filter_prs(items, filters, ops.local_slugs(states), pack.pack_slugs(entries))


def tool_status(key: str, cfg: Config, entries: list[PackEntry], states: Iterable[RepoState]) -> str:
    """The current state a tool of the Tools tab would act on, for the detail pane."""
    if key == "clone":
        if not entries:
            return "Pack data not loaded yet."
        missing = pack.missing_jobs(entries, cfg.root, cfg.clone_protocol)
        return f"Pack mods not checked out: {len(missing)}. Org repos are listed from GitHub when you pick them."
    if key == "jars":
        closed = sum(1 for e in entries if e.closed)
        return f"Closed-source mods in the pack: {closed}" if entries else "Pack data not loaded yet."
    if key == "clean":
        found = ops.built_jars(states, cfg.org)
        files = [p for _, jars in found for p in jars]
        size = render.megabytes(sum(ops.file_size(p) for p in files))
        return f"Now: {len(files)} built jars, {size}, in {len(found)} repos." if files else "No built jars now."
    if key == "skill":
        found = [
            f"{label} {'installed' if skill.installed(a) else 'not installed'}" for a, label in skill.AGENTS.items()
        ]
        return ", ".join(found) + "."
    return ""


def action_shown(action: str, view: str, ide_off: bool = False, repo_selected: bool = True) -> bool:
    """Whether an action belongs on a tab. The terminal UI's footer and the desktop UI's toolbar both ask this,
    so a tab or an action added later shows up the same way in both.

    repo_selected: a local checkout goes with the selected line (on PR tabs, the PR's repo is checked out here).
    """
    if view == "tools" and action in NOT_ON_TOOLS:
        return False
    if action in REPO_ONLY_ACTIONS:
        return view not in TABLES
    if action == "sort":
        return view != "pack"
    if action == "clone":
        return view == "pack"
    if action == "checkout":
        return view in PR_VIEWS
    if action == "toggle_team":
        return view == "reviews"
    if action in ("toggle_drafts", "pr_filters"):
        return view == "org"
    if action == "rescan":
        return view not in PR_VIEWS
    if action == "reload_prs":
        return view in ("mine", "reviews")
    if action == "reload_org":
        return view == "org"
    if action == "open_ide" and ide_off:
        return False
    if action in CHECKOUT_ACTIONS:
        return view not in PR_VIEWS or repo_selected
    return True


def repo_cells(st: RepoState, width: int = 48) -> list[Text]:
    return [
        render.name_text(st),
        Text(st.group, "dim"),
        render.branch_text(st),
        render.state_text(st),
        render.sync_text(st),
        render.daily_text(st),
        render.release_text(st),
        render.note_text(st.note),
        render.last_text(st, width),
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


def review_detail(item: ReviewItem, local: RepoState | None, k: dict[str, str]) -> RenderableType:
    head = Text.assemble((f"{item.repo} ", "bold"), render.pr_text(item.pr), f"  by {item.author}")
    where = Text(f"checked out at {local.path}", "green") if local else Text("not checked out", "dim")
    hint = f"Enter or {k['open_web']}: open in the browser   {k['toggle_team']}: show or hide team requests"
    hint = Text(f"{item.pr.url}   {hint}", "dim")
    return Group(head, Text(item.pr.title), where, hint)


def org_detail(item: ReviewItem, mark: str, local: RepoState | None, k: dict[str, str]) -> RenderableType:
    head = Text.assemble((f"{item.repo} ", "bold"), render.pr_text(item.pr), f"  by {item.author}")
    concern = {"author": "your PR", "requested": "your review is requested", "reviewed": "you reviewed it"}
    where = Text(f"checked out at {local.path}", "green") if local else Text("not checked out", "dim")
    if mark:
        where = Text.assemble((concern[mark], "bold"), "   ", where)
    hint = (
        f"Enter or {k['open_web']}: open   {k['checkout']}: check out   {k['toggle_drafts']}: drafts   "
        f"{k['pr_filters']}: show only   {k['reload_org']}: search again"
    )
    hint = Text(f"{item.pr.url}   {hint}", "dim")
    return Group(head, Text(item.pr.title), where, hint)


def mine_detail(item: ReviewItem, local: tuple[RepoState, str] | None, k: dict[str, str]) -> RenderableType:
    head = Text.assemble((f"{item.repo} ", "bold"), render.pr_text(item.pr))
    if local:
        where = Text(f"branch {local[1]} in {local[0].path}", "green")
    else:
        where = Text("branch not checked out here", "dim")
    hint = Text(f"{item.pr.url}   Enter or {k['open_web']}: open in the browser", "dim")
    return Group(head, Text(item.pr.title), where, hint)


def pack_detail(entry: PackEntry, info: PackInfo, k: dict[str, str]) -> RenderableType:
    release = f"{info.release or 'release'} {entry.release_version or '—'}"
    versions = f"  {entry.kind}  daily {entry.version or '—'}  {release}"
    lines = [render.pack_header(info), Text.assemble((entry.name, "bold"), versions)]
    lines.append(Text(f"licence: {entry.license or 'unknown'}   {entry.url or ''}", "dim"))
    if entry.dev_status or entry.note:
        lines.append(Text(" · ".join(part for part in (entry.dev_status, entry.note) if part), "dim italic"))
    if entry.local:
        lines.append(Text(f"checked out at {entry.local.path}", "green"))
    elif entry.status == "missing":
        at = f" @ {entry.branch}" if entry.branch else ""
        lines.append(Text(f"press {k['clone']} to clone {entry.repo}{at}", "yellow"))
    return Group(*lines)


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
        ("o", "ide", "Open in IntelliJ", "turned off in Settings" if ide_off else None),
        ("w", "web", f"Open PR #{pr.number}" if pr else "Open on GitHub", None if st.slug else "no GitHub remote"),
        ("e", "folder", "Open in file manager", None),
        ("t", "terminal", "Open terminal here", None),
        ("b", "main_branch", f"Set main branch (now {main})", None),
        ("u", "group", f"Set group (now {st.group})", None),
        ("h", "hide", "Hide this repo from the tool", None),
    ]


def tool_checkout() -> Path | None:
    """The checkout this tool runs from, which can update itself; None when installed as a package."""
    return skill.own_checkout()


def tool_behind(st: RepoState | None) -> int:
    """New commits for the tool's own checkout (st: that checkout, fetched)."""
    if st is None or st.error:
        return 0
    return st.main_behind if st.off_main else st.behind


def tool_entry(st: RepoState | None, updated: bool) -> tuple[str, str | None]:
    """Label of the Settings entry that updates the tool, and why it cannot be used now."""
    label = "Update NH Workbench"
    if tool_checkout() is None:
        return label, "installed as a package: uv tool upgrade or uvx --refresh"
    if updated:
        return label, "updated: restart the tool to use it"
    if st is None:
        return label, "checking for updates"
    if st.error or (st.note and st.note.kind == "failed"):
        return label, f"could not check: {st.error or st.note.text}"
    behind = tool_behind(st)
    if behind:
        return f"{label} ({behind} new commit{'s' if behind > 1 else ''})", None
    return label, f"up to date, {st.head or 'detached'} at {st.head_sha[:7]}"


async def update_tool(cfg: Config, st: RepoState | None) -> tuple[RepoState | None, str, bool]:
    """Pull the tool's own checkout. Returns its new state, what to tell (a warning), and whether it updated."""
    path = tool_checkout()
    if path is None or st is None or not st.tracked:
        return st, "", False
    subjects = await git.subjects_between(path, st.tracked, f"{st.tracked}@{{u}}") or []
    new = await ops.pull_repo(cfg, path)
    note = new.note
    if not note or note.kind != "updated":
        return new, f"Not updated: {note.text if note else 'nothing to pull'}", False
    if new.off_main:
        return (
            new,
            f"{new.tracked} updated in {path}, but the checkout is on {new.head}:"
            f" switch to {new.tracked} and restart the tool to use it",
            True,
        )
    latest = f": {subjects[0]}" + (f" and {len(subjects) - 1} more" if len(subjects) > 1 else "") if subjects else ""
    return new, f"Updated{latest}. Restart the tool to use it.", True


def no_cleanup_note(unpushed: list) -> str:
    kept = f" ({len(unpushed)} merged ones kept: local commits not in their PR)" if unpushed else ""
    return f"No merged branches to clean up{kept}"


def cleanup_body(safe: list[tuple[RepoState, Branch]], parked: dict[Path, RepoState]) -> Text:
    """The branches and remotes a cleanup deletes, one per line."""
    body = Text("\n").join(
        Text.assemble(
            (st.name, "bold"),
            f"  {br.name}",
            (f"  PR #{br.pr.number}" if br.pr else "", "dim"),
            (f"  checked out, switches to {st.tracked} first" if st.path in parked else "", "yellow"),
        )
        for st, br in safe
    )
    for st, remote in ops.orphaned_remotes(safe):
        body.append("\n").append_text(
            Text.assemble((st.name, "bold"), f"  remote {remote.name}", ("  no longer tracked", "dim"))
        )
    return body


def finish_cleanup(report: ops.CleanupReport) -> tuple[str, bool]:
    """Append the undo commands to the log; what to tell after a cleanup, and whether something failed."""
    log_file = config.cache_dir() / "deleted-branches.log"
    if report.restore:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.writelines(f"{datetime.now():%Y-%m-%d %H:%M} {line}\n" for line in report.restore)
    note = f"Deleted {report.branches} branches and {report.remotes} remotes, undo commands in {log_file}"
    failed = len(report.failed)
    return note + (f"; {failed} failed" if failed else ""), bool(failed)


def to_main_body(candidates: Iterable[RepoState]) -> Text:
    return Text("\n").join(
        Text.assemble((st.name, "bold"), f"  {st.head} → {st.tracked}", (f"  PR #{st.current.pr.number}", "dim"))
        for st in candidates
    )
