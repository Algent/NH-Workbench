import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

from . import config, github, ops, pack, render
from .config import Config
from .models import PackInfo, RepoState

app = typer.Typer(add_completion=False, help="Keep track of many GTNH mod checkouts.")
out = Console()
err = Console(stderr=True)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    root: Annotated[Path | None, typer.Option("--root", help="Folder holding the checkouts.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log every command that is run.")] = False,
) -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = config.load(root)
    except config.ConfigError as e:
        err.print(f"[bold red]{e}")
        raise typer.Exit(2) from e
    # The UI asks for the folder itself; commands need it up front.
    if not cfg.root.is_dir() and ctx.invoked_subcommand is not None:
        err.print(f'[bold red]Folder with your checkouts not found: "{cfg.root}"')
        err.print(
            f'Pass --root PATH, set root in "{config.config_path()}", or start gtnh-dev without a command to pick it.'
        )
        raise typer.Exit(2)
    ctx.obj = cfg

    if ctx.invoked_subcommand is None:
        if verbose:
            config.cache_dir().mkdir(parents=True, exist_ok=True)
            logging.basicConfig(filename=config.cache_dir() / "tui.log", level=logging.DEBUG)
        from .tui import RepoApp

        RepoApp(cfg, root_override=root).run()
    elif verbose:
        logging.basicConfig(stream=sys.stderr, level=logging.DEBUG, format="%(levelname)-7s %(message)s")


async def _each(cfg: Config, label: str, worker: Callable[[Path], Awaitable[RepoState]]) -> list[RepoState]:
    paths = ops.discover(cfg)
    columns = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn())
    with Progress(*columns, console=err, transient=True) as progress:
        task = progress.add_task(label, total=len(paths))
        states = await ops.run_all(paths, worker, cfg.concurrency, lambda _: progress.advance(task))
    return sorted(states, key=ops.by_repo)


def _show(states: list[RepoState], shown: list[RepoState] | None = None, info: PackInfo | None = None) -> None:
    out.print(render.repo_table(states if shown is None else shown, release=info.release if info else None))
    out.print()
    if info:
        out.print(render.pack_header(info))
    out.print(render.summary(states))


async def _attach_pack(cfg: Config, states: list[RepoState], refresh: bool = False) -> PackInfo:
    """Load pack data and copy versions and release-branch progress onto the local repos."""
    with err.status("Loading pack data..."):
        info = await pack.load(cfg, force=refresh)
    pack.link_local(info.entries, states)
    branch = pack.release_branch(info.release) if info.release else ""
    await ops.pack_progress(states, branch, cfg.concurrency)
    return info


@app.command()
def reviews(
    team: Annotated[bool, typer.Option("--team", help="Also list requests that reach you through a team.")] = False,
) -> None:
    """Open pull requests waiting on your review."""
    with err.status("Asking GitHub for review requests..."):
        items, error = asyncio.run(github.fetch_reviews(team))
    if error and not items:
        err.print(f"[bold red]Review requests unavailable: {error}")
        raise typer.Exit(1)
    if error:
        err.print(f"[yellow]Team review requests are incomplete: {error}")
    direct = sum(1 for item in items if item.direct)
    out.print(render.reviews_table([i for i in items if team or i.direct], width=max(30, out.width - 90)))
    more = f"{len(items) - direct} more through teams" if team else "--team also lists requests through teams"
    out.print(f"\n[dim]{direct} asked of you · {more}")


@app.command()
def prs(
    ctx: typer.Context,
    mine: Annotated[bool, typer.Option("--mine", help="Your own open PRs.")] = False,
    org: Annotated[bool, typer.Option("--org", help="Every open PR of the org (saved for 30 minutes).")] = False,
    team: Annotated[bool, typer.Option("--team", help="Review requests: also the ones through a team.")] = False,
    max_size: Annotated[int, typer.Option("--max-size", help="Only PRs under this many changed lines.")] = 0,
    sort: Annotated[str, typer.Option("--sort", help="updated, small, large or repo.")] = "updated",
    local: Annotated[bool, typer.Option("--local", help="Only repos checked out here.")] = False,
    in_pack: Annotated[bool, typer.Option("--pack", help="Only pack mods.")] = False,
    unreviewed: Annotated[bool, typer.Option("--unreviewed", help="Only PRs without any review.")] = False,
    stale: Annotated[
        bool, typer.Option("--stale", help=f"Only PRs not updated for {github.STALE_DAYS}+ days.")
    ] = False,
    drafts: Annotated[bool, typer.Option("--drafts", help="With --org: include drafts.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="With --org: search again even if recent.")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Show at most this many, 0 for all.")] = 0,
) -> None:
    """Open pull requests: review requests by default, or yours, or the whole org's; sorted and filtered."""
    cfg: Config = ctx.obj
    if mine and org:
        err.print("[bold red]Pick one of --mine and --org")
        raise typer.Exit(2)
    if sort not in render.PR_SORTS:
        err.print(f"[bold red]Unknown sort {sort!r}, use one of: {', '.join(render.PR_SORTS)}")
        raise typer.Exit(2)
    reviewed: set[str] = set()
    if org:
        found = None if refresh else github.cached_org(cfg.org)
        if found is None or time.time() - found.fetched > github.ORG_MAX_AGE:
            with err.status(f"Searching all open PRs of {cfg.org}, about 20 seconds..."):
                found = asyncio.run(github.fetch_org(cfg.org))
        items, error, reviewed = found.items, found.error, found.reviewed
        if not drafts:
            items = [i for i in items if not i.pr.draft]
    elif mine:
        with err.status("Asking GitHub for your PRs..."):
            items, error = asyncio.run(github.fetch_mine())
    else:
        with err.status("Asking GitHub for review requests..."):
            items, error = asyncio.run(github.fetch_reviews(team))
    if error and not items:
        err.print(f"[bold red]PRs unavailable: {error}")
        raise typer.Exit(1)
    if error:
        err.print(f"[yellow]List may be incomplete: {error}")

    wanted = [f for f, on in (("local", local), ("pack", in_pack), ("unreviewed", unreviewed), ("stale", stale)) if on]
    slugs, pack_slugs = set(), set()
    if local or in_pack:
        states = asyncio.run(_each(cfg, "Scanning", lambda p: ops.scan_repo(cfg, p))) if local else []
        slugs = ops.local_slugs(states)
        if in_pack:
            with err.status("Loading pack data..."):
                info = asyncio.run(pack.load(cfg))
            pack.link_local(info.entries, states)
            pack_slugs = pack.pack_slugs(info.entries)
    if wanted:
        if unreviewed and any(i.pr.reviews is None for i in items):
            err.print("[yellow]Some PRs have no review count yet (older saved search): --refresh gets them")
        items = github.filter_prs(items, wanted, slugs, pack_slugs)
    if max_size:
        items = [i for i in items if i.pr.size < max_size]
    key = render.PR_SORTS[sort][1]
    items = sorted(items, key=key) if key else items
    total = len(items)
    items = items[:limit] if limit else items

    width = max(30, out.width - 90)
    if org:
        rows = (render.org_row(i, "reviewed" if i.pr.url in reviewed else "", width) for i in items)
        out.print(render.pr_table(render.ORG_COLUMNS, rows))
    elif mine:
        out.print(render.pr_table(render.MINE_COLUMNS, (render.mine_row(i, width=width) for i in items)))
    else:
        out.print(render.reviews_table(items, width))
    shown = f"{len(items)} of {total}" if len(items) < total else str(total)
    out.print(f"\n[dim]{shown} PRs")


async def _with_prs(states: list[RepoState]) -> None:
    with err.status("Asking GitHub about branches..."):
        data = await github.fetch_prs(states)
    github.attach(states, data)
    if data.error:
        err.print(f"[yellow]PR status unavailable: {data.error}")


@app.command()
def status(
    ctx: typer.Context,
    off_main: Annotated[bool, typer.Option("--off-main", help="Only repos not on their main branch.")] = False,
    behind: Annotated[bool, typer.Option("--behind", help="Only repos behind their upstream.")] = False,
    dirty: Annotated[bool, typer.Option("--dirty", help="Only repos with uncommitted tracked changes.")] = False,
    group: Annotated[str | None, typer.Option("--group", help="Only this group (gtnh, other, a subfolder).")] = None,
    fetch: Annotated[bool, typer.Option("--fetch", help="Fetch every repo first.")] = False,
    with_pack: Annotated[
        bool, typer.Option("--pack", help="Add the version in the daily and in the latest release.")
    ] = False,
) -> None:
    """One line per repo: branch, working tree state, ahead/behind, last commit."""
    cfg: Config = ctx.obj
    worker = ops.fetch_repo if fetch else ops.scan_repo

    async def collect() -> tuple[list[RepoState], PackInfo | None]:
        states = await _each(cfg, "Fetching" if fetch else "Scanning", lambda p: worker(cfg, p))
        try:
            return states, await _attach_pack(cfg, states) if with_pack else None
        except pack.PackError as e:
            err.print(f"[yellow]Pack data unavailable: {e}")
            return states, None

    states, info = asyncio.run(collect())
    shown = [
        st
        for st in states
        if (not off_main or st.off_main)
        and (not behind or st.is_behind)
        and (not dirty or st.dirty)
        and (not group or st.group.lower() == group.lower())
    ]
    for st in states:
        if st.note and st.note.text == "fetched":
            st.note = None
    _show(states, shown, info)


@app.command()
def fetch(ctx: typer.Context) -> None:
    """Fetch all remotes of every repo (no branch is moved)."""
    cfg: Config = ctx.obj
    states = asyncio.run(_each(cfg, "Fetching", lambda p: ops.fetch_repo(cfg, p)))
    _show(states)
    raise typer.Exit(1 if any(st.note and st.note.kind == "failed" for st in states) else 0)


@app.command()
def pull(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would move, based on the last fetch. Touches nothing.")
    ] = False,
) -> None:
    """Fetch, then fast-forward the main branch of every repo where that is safe."""
    cfg: Config = ctx.obj
    label = "Checking" if dry_run else "Pulling"
    states = asyncio.run(_each(cfg, label, lambda p: ops.pull_repo(cfg, p, dry_run)))
    _show(states)
    if dry_run:
        out.print("[dim]Dry run: nothing was fetched or moved, counts are as of the last fetch.")
    raise typer.Exit(1 if any(st.note and st.note.kind == "failed" for st in states) else 0)


@app.command()
def branches(
    ctx: typer.Context,
    cleanup: Annotated[bool, typer.Option("--cleanup", help="Offer to delete branches whose PR is merged.")] = False,
) -> None:
    """Local branches other than main, with the state of their pull request."""
    cfg: Config = ctx.obj

    async def collect() -> tuple[list[RepoState], list, list]:
        states = await _each(cfg, "Scanning", lambda p: ops.scan_repo(cfg, p))
        await _with_prs(states)
        safe, unpushed = await ops.cleanup_candidates(states) if cleanup else ([], [])
        return states, safe, unpushed

    states, safe, unpushed = asyncio.run(collect())
    if not cleanup:
        with_branches = [st for st in states if st.extra_branches]
        out.print(render.branches_table(with_branches, width=max(24, out.width - 130)))
        count = sum(len(st.extra_branches) for st in with_branches)
        out.print(f"\n[dim]{count} branches in {len(with_branches)} repos")
        return

    for st, br, extra in unpushed:
        why = "could not be compared with it" if extra is None else f"has {extra} local commits not in it"
        out.print(f"[yellow]kept[/] {st.name} {br.name}: PR #{br.pr.number} merged, but the branch {why}")
    if not safe:
        out.print("Nothing to clean up.")
        return
    out.print("Merged and fully contained in their PR:")
    for st, br in safe:
        out.print(f"  {st.name} [bold]{br.name}[/] [dim]{br.sha[:10]} PR #{br.pr.number}")
    remotes = ops.orphaned_remotes(safe)
    if remotes:
        out.print("Fork remotes nothing else would track afterwards (removed too):")
        for st, remote in remotes:
            out.print(f"  {st.name} [bold]{remote.name}[/] [dim]{remote.url}")
    if not typer.confirm(f"Delete these {len(safe)} local branches?"):
        raise typer.Exit(0)

    report = asyncio.run(ops.cleanup(safe))
    out.print(f"[green]Deleted {report.branches} branches and {report.remotes} remotes.[/] To undo:")
    for line in report.restore:
        out.print(f"  [dim]{line}", soft_wrap=True)
    for line in report.failed:
        out.print(f"[red]failed[/] {line}")


@app.command(name="to-main")
def to_main(ctx: typer.Context) -> None:
    """Switch repos parked on a merged PR branch back to main, then fast-forward it."""
    cfg: Config = ctx.obj

    async def collect() -> list[RepoState]:
        states = await _each(cfg, "Scanning", lambda p: ops.scan_repo(cfg, p))
        await _with_prs(states)
        return ops.to_main_candidates(states)

    candidates = asyncio.run(collect())
    if not candidates:
        out.print("No repo is parked on a merged branch with a clean working tree.")
        return
    for st in candidates:
        out.print(f"  {st.name} [bold]{st.head}[/] → {st.tracked} [dim]PR #{st.current.pr.number} merged")
    if not typer.confirm(f"Switch these {len(candidates)} repos back to main?"):
        raise typer.Exit(0)

    async def switch() -> list[RepoState]:
        return await ops.run_all(candidates, lambda st: ops.to_main(cfg, st), cfg.concurrency)

    done = asyncio.run(switch())
    out.print(render.repo_table(done))
    out.print("\n[dim]The merged branches are still there: `gtnh-dev branches --cleanup` removes them.")
    raise typer.Exit(1 if any(st.note and st.note.kind == "failed" for st in done) else 0)


async def _load_pack(cfg: Config, refresh: bool) -> tuple[PackInfo, list[RepoState]]:
    states = await _each(cfg, "Scanning", lambda p: ops.scan_repo(cfg, p))
    await _with_prs(states)  # canonical names, so renamed repos still match
    return await _attach_pack(cfg, states, refresh), states


@app.command(name="pack")
def pack_command(
    ctx: typer.Context,
    missing: Annotated[bool, typer.Option("--missing", help="Only mods that are not checked out.")] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-download pack data now (otherwise at most once an hour).")
    ] = False,
) -> None:
    """Mods of the current daily and the latest release, and whether you have them checked out."""
    cfg: Config = ctx.obj
    try:
        info, states = asyncio.run(_load_pack(cfg, refresh))
    except pack.PackError as e:
        err.print(f"[bold red]{e}")
        raise typer.Exit(1) from e
    entries = info.entries
    out.print(render.pack_table([e for e in entries if not missing or not e.local], info))
    out.print()
    out.print(render.pack_header(info))
    counts = {s: sum(1 for e in entries if e.status == s) for s in ("cloned", "missing", "closed", "unknown")}
    daily = sum(1 for e in entries if e.version)
    extra = sum(1 for st in states if not st.in_pack)
    out.print(
        f"[dim]{daily} mods in daily, {len(entries) - daily} only in {info.release or 'the release'} · "
        f"{counts['cloned']} cloned · {counts['missing']} missing · {counts['closed']} closed source · "
        f"{counts['unknown']} source unknown · {extra} local repos not in the pack"
    )


@app.command()
def jars(ctx: typer.Context) -> None:
    """Download the jars of closed-source pack mods into _GTNH_Closed_Source under the root."""
    cfg: Config = ctx.obj

    async def work() -> pack.JarReport:
        info = await pack.load(cfg)
        with err.status("Downloading jars..."):
            return await pack.download_closed(info.entries, cfg.root / pack.JAR_FOLDER)

    try:
        report = asyncio.run(work())
    except pack.PackError as e:
        err.print(f"[bold red]{e}")
        raise typer.Exit(1) from e
    for name in report.downloaded:
        out.print(f"[green]downloaded[/] {name}")
    for line in report.failed:
        out.print(f"[red]failed[/] {line}")
    out.print(f"[dim]{report.summary} in {cfg.root / pack.JAR_FOLDER}")
    raise typer.Exit(1 if report.failed else 0)


@app.command()
def clone(
    ctx: typer.Context,
    names: Annotated[list[str], typer.Argument(help="Pack mod or repository names.")],
) -> None:
    """Clone pack mods into the root folder."""
    cfg: Config = ctx.obj

    async def work() -> int:
        entries = (await pack.load(cfg)).entries
        failed = 0
        for name in names:
            entry = pack.find(entries, name)
            if not entry:
                err.print(f"[red]{name}: not a mod of the daily or the latest release")
                failed += 1
                continue
            with err.status(f"Cloning {entry.repo or entry.name}..."):
                res = await pack.clone(entry, cfg.root, cfg.clone_protocol)
            if res.ok:
                out.print(f"[green]cloned[/] {entry.repo}")
            else:
                out.print(f"[red]failed[/] {entry.name}: {res.message}")
                failed += 1
        return failed

    try:
        failed = asyncio.run(work())
    except pack.PackError as e:
        err.print(f"[bold red]{e}")
        raise typer.Exit(1) from e
    raise typer.Exit(1 if failed else 0)
