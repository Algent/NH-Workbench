from collections.abc import Iterable
from datetime import UTC, datetime

from rich.table import Table
from rich.text import Text

from .models import Branch, Outcome, PackEntry, PackInfo, PrInfo, RepoState, ReviewItem

NOTE_STYLE = {
    "updated": "bold green",
    "current": "dim",
    "skipped": "yellow",
    "failed": "bold red",
    "planned": "cyan",
    "info": "dim",
}
_PR_STATE = {"OPEN": ("open", "green"), "MERGED": ("merged", "magenta"), "CLOSED": ("closed", "red")}
_REVIEW = {
    "APPROVED": ("approved", "green"),
    "CHANGES_REQUESTED": ("changes requested", "red"),
    "REVIEW_REQUIRED": ("review required", "yellow"),
}
_CHECKS = {
    "SUCCESS": ("CI ok", "green"),
    "FAILURE": ("CI fail", "red"),
    "ERROR": ("CI error", "red"),
    "PENDING": ("CI pending", "yellow"),
    "EXPECTED": ("CI pending", "yellow"),
}
_PACK_STATUS = {"cloned": "green", "missing": "yellow", "closed": "dim", "unknown": "dim italic"}
_DAY = 86400


def ago(when: datetime | None) -> str:
    if not when:
        return ""
    secs = max((datetime.now(UTC) - when).total_seconds(), 0)
    for limit, unit, label in ((3600, 60, "m"), (_DAY, 3600, "h"), (14 * _DAY, _DAY, "d"), (70 * _DAY, 7 * _DAY, "w")):
        if secs < limit:
            return f"{int(secs // unit)}{label}"
    if secs < 730 * _DAY:
        return f"{int(secs // (30 * _DAY))}mo"
    return f"{int(secs // (365 * _DAY))}y"


def branch_text(st: RepoState) -> Text:
    if st.error:
        return Text("error", "bold red")
    if st.head is None:
        return Text(f"detached {st.head_sha[:8]}", "bold red")
    return Text(st.head, "bold yellow" if st.off_main else "green")


def state_text(st: RepoState) -> Text:
    if st.error:
        return Text(st.error, "red")
    parts = []
    if st.in_progress:
        parts.append(Text(st.in_progress.upper(), "bold magenta"))
    if st.changed:
        parts.append(Text(f"mod {st.changed}", "bold red"))
    if st.untracked:
        parts.append(Text(f"new {st.untracked}", "dim"))
    if st.stash:
        parts.append(Text(f"stash {st.stash}", "cyan"))
    return Text(" ").join(parts) if parts else Text("clean", "dim green")


def track_text(ahead: int, behind: int, gone: bool, upstream: str | None) -> Text:
    if gone:
        return Text("gone", "red")
    if not upstream:
        return Text("local", "dim")
    parts = []
    if ahead:
        parts.append(Text(f"↑{ahead}", "cyan"))
    if behind:
        parts.append(Text(f"↓{behind}", "bold yellow"))
    return Text(" ").join(parts) if parts else Text("·", "dim")


def sync_text(st: RepoState) -> Text:
    if st.error:
        return Text("")
    if st.head is None:
        text = Text("")
    else:
        text = track_text(st.ahead, st.behind, st.gone, st.upstream)
    if st.main_behind:
        text.append(f" {st.tracked} ↓{st.main_behind}", "yellow")
    return text


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def last_text(st: RepoState, width: int = 48) -> Text:
    return Text.assemble((f"{ago(st.date):>4} ", "dim"), clip(st.subject, width))


def note_text(note: Outcome | None) -> Text:
    return Text(note.text, NOTE_STYLE.get(note.kind, "")) if note else Text("")


def pr_text(pr: PrInfo | None) -> Text:
    if not pr:
        return Text("")
    label, style = ("draft", "dim") if pr.draft and pr.state == "OPEN" else _PR_STATE.get(pr.state, (pr.state, ""))
    text = Text.assemble((f"#{pr.number} ", "bold"), (label, style))
    if pr.state == "OPEN":
        for mapping, key in ((_REVIEW, pr.review), (_CHECKS, pr.checks)):
            if key in mapping:
                text.append(" · ").append(*mapping[key])
    return text


def daily_text(st: RepoState) -> Text:
    """Version in the daily, plus how many commits main has that it lacks."""
    text = version_text(st.daily) if st.in_pack else Text("")
    if st.daily_ahead:
        text.append(f" +{st.daily_ahead}", "bold cyan")
    return text


def release_text(st: RepoState) -> Text:
    """Released version, plus how many commits the release branch gained since."""
    text = version_text(st.release) if st.in_pack else Text("")
    if st.release_ahead:
        text.append(f" +{st.release_ahead}", "bold cyan")
    return text


def repo_table(states: Iterable[RepoState], release: str | None = None) -> Table:
    """One line per repo. Pass the release name to add the Daily and release version columns."""
    states = list(states)
    notes = any(st.note for st in states)
    groups = len({st.group for st in states}) > 1
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("Repo", *(["Group"] if groups else []), "Branch", "State", "Sync"):
        table.add_column(column, no_wrap=True)
    if release is not None:
        table.add_column("Daily", no_wrap=True)
        table.add_column(release or "Release", no_wrap=True)
    if notes:
        table.add_column("Result", no_wrap=True)
    table.add_column("Last commit", no_wrap=True, overflow="ellipsis")
    for st in states:
        row = [Text(st.name, "bold"), *([Text(st.group, "dim")] if groups else [])]
        row += [branch_text(st), state_text(st), sync_text(st)]
        if release is not None:
            row += [daily_text(st), release_text(st)]
        if notes:
            row.append(note_text(st.note))
        table.add_row(*row, last_text(st))
    return table


def summary(states: Iterable[RepoState]) -> Text:
    states = list(states)
    parts = [
        f"{len(states)} repos",
        f"{sum(st.off_main for st in states)} off main",
        f"{sum(st.is_behind for st in states)} behind",
        f"{sum(st.dirty for st in states)} dirty",
    ]
    kinds = (("updated", "updated"), ("planned", "would update"), ("skipped", "skipped"), ("failed", "failed"))
    for kind, label in kinds:
        count = sum(1 for st in states if st.note and st.note.kind == kind)
        if count:
            parts.append(f"{count} {label}")
    errors = sum(1 for st in states if st.error)
    if errors:
        parts.append(f"{errors} unreadable")
    return Text(" · ".join(parts), "dim")


def branch_row(st: RepoState, br: Branch, width: int = 60) -> list[Text]:
    name = Text(("* " if br.name == st.head else "  ") + br.name, "bold yellow" if br.name == st.head else "")
    detail = br.pr.title if br.pr else br.subject
    return [
        name,
        track_text(br.ahead, br.behind, br.gone, br.upstream),
        Text(ago(br.date), "dim"),
        pr_text(br.pr),
        Text(clip(detail, width), "dim"),
    ]


def branches_table(states: Iterable[RepoState], width: int = 60) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("Repo", "Branch", "Sync", "Age", "PR"):
        table.add_column(column, no_wrap=True)
    table.add_column("Title / last commit", no_wrap=True, overflow="ellipsis")
    for st in states:
        for i, br in enumerate(st.extra_branches):
            table.add_row(Text(st.name if i == 0 else "", "bold"), *branch_row(st, br, width))
    return table


def review_row(item: ReviewItem, width: int = 70) -> list[Text]:
    return [
        Text("you", "bold green") if item.direct else Text("team", "dim"),
        Text(item.repo.partition("/")[2] or item.repo, "bold"),
        pr_text(item.pr),
        Text(item.author),
        Text(ago(item.updated), "dim"),
        Text(clip(item.pr.title, width)),
    ]


REVIEW_COLUMNS = ("For", "Repo", "PR", "Author", "Updated", "Title")


def reviews_table(items: Iterable[ReviewItem], width: int = 70) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in REVIEW_COLUMNS:
        table.add_column(column, no_wrap=True)
    for item in items:
        table.add_row(*review_row(item, width))
    return table


def pack_status_text(entry: PackEntry) -> Text:
    status = entry.status
    if entry.local:
        return Text(clip(f"cloned ({entry.local.head or 'detached'})", 34), _PACK_STATUS["cloned"])
    if status == "missing" and entry.private:
        return Text("missing (private)", _PACK_STATUS["missing"])
    label = {"closed": "closed source", "unknown": "source unknown"}.get(status, status)
    return Text(label, _PACK_STATUS[status])


def version_text(version: str) -> Text:
    return Text(clip(version, 24)) if version else Text("—", "dim")


def pack_row(entry: PackEntry) -> list[Text]:
    source = entry.repo or ""
    if entry.branch:
        source += f" @ {entry.branch}"
    return [
        Text(clip(entry.name, 36), "bold"),
        Text(entry.kind, "" if entry.kind == "github" else "cyan"),
        version_text(entry.version),
        release_text(entry.local) if entry.local else version_text(entry.release_version),
        pack_status_text(entry),
        Text(clip(source or (entry.url or ""), 56), "dim"),
    ]


def pack_header(info: PackInfo) -> Text:
    """Which daily and which release the pack data describes, and how fresh it is."""
    text = Text.assemble((f"Daily #{info.daily_build}" if info.daily_build else "Daily", "bold"))
    text.append(f" {info.daily_config}").append(f" · {ago(info.daily_updated)} ago", "dim")
    if info.daily_attempt and info.daily_attempt != info.daily_build:
        text.append(f" (#{info.daily_attempt} did not succeed)", "yellow")
    if info.release:
        text.append("   ").append(f"Release {info.release}", "bold")
        text.append(f" · {ago(info.release_updated)} ago", "dim")
    return text.append(f"   checked {ago(info.fetched)} ago", "dim")


def pack_labels(info: PackInfo | None) -> dict[str, str]:
    """Headers of the two version columns, naming the daily build and the release they describe."""
    daily = f"Daily (#{info.daily_build})" if info and info.daily_build else "Daily"
    release = f"Release ({info.release})" if info and info.release else "Release"
    return {"Daily": daily, "Release": release}


def pack_table(entries: Iterable[PackEntry], info: PackInfo | None = None) -> Table:
    labels = pack_labels(info)
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("Mod", "Kind", labels["Daily"], labels["Release"], "Local"):
        table.add_column(column, no_wrap=True)
    table.add_column("Source", no_wrap=True, overflow="ellipsis")
    for entry in entries:
        table.add_row(*pack_row(entry))
    return table
