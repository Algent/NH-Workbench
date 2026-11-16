import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

from . import config, github, ops, pack, render
from .config import Config
from .models import RepoState

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
    if not cfg.root.is_dir():
        err.print(f'[bold red]Root folder not found: "{cfg.root}"')
        raise typer.Exit(2)
    ctx.obj = cfg

    if ctx.invoked_subcommand is None:
        if verbose:
            config.cache_dir().mkdir(parents=True, exist_ok=True)
            logging.basicConfig(filename=config.cache_dir() / "tui.log", level=logging.DEBUG)
        from .tui import RepoApp

        RepoApp(cfg).run()
    elif verbose:
        logging.basicConfig(stream=sys.stderr, level=logging.DEBUG, format="%(levelname)-7s %(message)s")


async def _each(cfg: Config, label: str, worker: Callable[[Path], Awaitable[RepoState]]) -> list[RepoState]:
    paths = ops.discover(cfg)
    columns = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn())
    with Progress(*columns, console=err, transient=True) as progress:
        task = progress.add_task(label, total=len(paths))
        return await ops.run_all(paths, worker, cfg.concurrency, lambda _: progress.advance(task))


def _show(states: list[RepoState], shown: list[RepoState] | None = None, pack_column: bool = False) -> None:
    out.print(render.repo_table(states if shown is None else shown, pack=pack_column))
    out.print()
    out.print(render.summary(states))


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
) -> None:
    """One line per repo: branch, working tree state, ahead/behind, last commit."""
    cfg: Config = ctx.obj
    worker = ops.fetch_repo if fetch else ops.scan_repo
    states = asyncio.run(_each(cfg, "Fetching" if fetch else "Scanning", lambda p: worker(cfg, p)))
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
    _show(states, shown)


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

    for st, br in unpushed:
        out.print(f"[yellow]kept[/] {st.name} {br.name}: PR #{br.pr.number} merged, but has commits not in it")
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


async def _load_pack(cfg: Config, refresh: bool) -> tuple[list, list[RepoState]]:
    states = await _each(cfg, "Scanning", lambda p: ops.scan_repo(cfg, p))
    await _with_prs(states)  # canonical names, so renamed repos still match
    with err.status("Loading the daily manifest..."):
        entries = await pack.load(cfg, force=refresh)
    pack.link_local(entries, states)
    return entries, states


@app.command(name="pack")
def pack_command(
    ctx: typer.Context,
    missing: Annotated[bool, typer.Option("--missing", help="Only mods that are not checked out.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-download the manifest now.")] = False,
) -> None:
    """Mods of the current daily build and whether you have them checked out."""
    cfg: Config = ctx.obj
    try:
        entries, states = asyncio.run(_load_pack(cfg, refresh))
    except pack.PackError as e:
        err.print(f"[bold red]{e}")
        raise typer.Exit(1) from e
    out.print(render.pack_table([e for e in entries if not missing or not e.local]))
    counts = {s: sum(1 for e in entries if e.status == s) for s in ("cloned", "missing", "closed", "unknown")}
    extra = sum(1 for st in states if not st.pack)
    out.print(
        f"\n[dim]{len(entries)} mods in daily · {counts['cloned']} cloned · {counts['missing']} missing · "
        f"{counts['closed']} closed source · {counts['unknown']} source unknown · {extra} local repos not in the pack"
    )


@app.command()
def clone(
    ctx: typer.Context,
    names: Annotated[list[str], typer.Argument(help="Pack mod or repository names.")],
) -> None:
    """Clone pack mods into the root folder."""
    cfg: Config = ctx.obj

    async def work() -> int:
        entries = await pack.load(cfg)
        failed = 0
        for name in names:
            entry = pack.find(entries, name)
            if not entry:
                err.print(f"[red]{name}: not a mod of the daily build")
                failed += 1
                continue
            with err.status(f"Cloning {entry.repo or entry.name}..."):
                res = await pack.clone(entry, cfg.root)
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
