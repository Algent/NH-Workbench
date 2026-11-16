import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import git
from .config import Config
from .models import Branch, Outcome, Remote, RepoState


def _subdirs(folder: Path) -> list[Path]:
    return [p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")]


def discover(cfg: Config) -> list[Path]:
    """Repos directly under the root, plus one level down inside plain folders (which act as groups)."""
    found = []
    for child in _subdirs(cfg.root):
        found += [child] if (child / ".git").exists() else [p for p in _subdirs(child) if (p / ".git").exists()]
    return sorted((p for p in found if not cfg.repo(p.name).ignore), key=lambda p: p.name.lower())


def group_of(cfg: Config, st: RepoState) -> str:
    explicit = cfg.repo(st.name).group
    if explicit:
        return explicit
    if st.path.parent != cfg.root:
        return st.path.parent.name
    owner = (st.origin.owner if st.origin else None) or ""
    return "gtnh" if owner.lower() == cfg.org.lower() else "other"


async def run_all[T, R](
    items: Iterable[T], worker: Callable[[T], Awaitable[R]], limit: int, on_done: Callable[[R], None] | None = None
) -> list[R]:
    sem = asyncio.Semaphore(limit)

    async def one(item: T) -> R:
        async with sem:
            result = await worker(item)
        if on_done:
            on_done(result)
        return result

    return list(await asyncio.gather(*(one(i) for i in items)))


async def scan_repo(cfg: Config, path: Path) -> RepoState:
    st = await git.read_state(path, cfg.repo(path.name).tracked_branch, cfg.slug_branches)
    st.group = group_of(cfg, st)
    return st


async def fetch_repo(cfg: Config, path: Path) -> RepoState:
    st = await scan_repo(cfg, path)
    if st.error:
        return st
    outcome = await git.fetch(st, cfg.prune)
    st = await scan_repo(cfg, path)
    st.note = outcome
    return st


async def pull_repo(cfg: Config, path: Path, dry_run: bool = False) -> RepoState:
    """Fetch, then fast-forward what is safe. A dry run touches nothing, not even remote-tracking refs."""
    st = await scan_repo(cfg, path) if dry_run else await fetch_repo(cfg, path)
    if st.error or (st.note and st.note.kind in ("failed", "skipped")):
        return st
    warning = st.note.text if st.note and st.note.text != "fetched" else ""
    outcome = await git.safe_pull(st, dry_run)
    if outcome.kind == "updated":
        st = await scan_repo(cfg, path)
    if warning:
        outcome = Outcome(outcome.kind, f"{outcome.text} ({warning})", outcome.commits)
    st.note = outcome
    return st


async def release_progress(states: Iterable[RepoState], branch: str, limit: int = 8) -> None:
    """Count the commits each repo's release branch gained since the version that was released."""

    async def one(st: RepoState) -> None:
        st.release_ahead = None
        if st.release and branch in st.release_branches:
            st.release_ahead = await git.commits_not_in(
                st.path, f"refs/tags/{st.release}", f"refs/remotes/origin/{branch}"
            )

    await run_all(states, one, limit)


def to_main_candidates(states: Iterable[RepoState]) -> list[RepoState]:
    """Repos parked on a branch whose PR is merged, with nothing uncommitted in the way."""
    return [
        st
        for st in states
        if st.off_main and not st.dirty and st.current and st.current.pr and st.current.pr.state == "MERGED"
    ]


async def to_main(cfg: Config, st: RepoState) -> RepoState:
    """Check out the main branch, then fetch and fast-forward it. The old branch is left for cleanup."""
    res = await git.switch(st.path, st.tracked)
    if not res.ok:
        new = await scan_repo(cfg, st.path)
        new.note = Outcome("failed", f"switch failed: {res.message}")
        return new
    new = await pull_repo(cfg, st.path)
    if new.note and new.note.kind != "failed":
        new.note = Outcome("updated", f"on {st.tracked}, {new.note.text}", new.note.commits)
    return new


async def cleanup_candidates(states: Iterable[RepoState]) -> tuple[list[tuple[RepoState, Branch]], list[tuple]]:
    """Split merged-PR branches into safe to delete and kept ones.

    Kept entries are (repo, branch, n): n local commits are not in the PR, or None if that could not be checked.
    """
    safe, kept = [], []
    for st in states:
        for br in st.branches:
            pr = br.pr
            if br.name in (st.head, st.tracked) or not pr or pr.state != "MERGED":
                continue
            extra = 0
            if br.sha != pr.head_sha:
                # The PR may have moved on after the last local fetch of its branch.
                if not await git.has_commit(st.path, pr.head_sha) and st.origin:
                    await git.fetch_pr_head(st.path, st.origin.name, pr.number)
                extra = await git.commits_not_in(st.path, pr.head_sha, br.sha)
            if extra == 0:
                safe.append((st, br))
            else:
                kept.append((st, br, extra))
    return safe, kept


def orphaned_remotes(doomed: Iterable[tuple[RepoState, Branch]]) -> list[tuple[RepoState, Remote]]:
    """Fork remotes that no local branch tracks any more once the given branches are deleted."""
    by_repo: dict[Path, tuple[RepoState, set[str]]] = {}
    for st, br in doomed:
        by_repo.setdefault(st.path, (st, set()))[1].add(br.name)
    orphans = []
    for st, names in by_repo.values():
        primary = st.origin.name if st.origin else None
        used = {st.remote_of(b) for b in st.branches if b.name in names} - {None, primary}
        still_used = {st.remote_of(b) for b in st.branches if b.name not in names}
        orphans += [(st, st.remotes[name]) for name in sorted(used - still_used)]
    return orphans


@dataclass
class CleanupReport:
    branches: int = 0
    remotes: int = 0
    restore: list[str] = field(default_factory=list)  # commands that undo each deletion
    failed: list[str] = field(default_factory=list)


async def cleanup(safe: Iterable[tuple[RepoState, Branch]]) -> CleanupReport:
    report = CleanupReport()
    deleted = []
    for st, br in safe:
        res = await git.delete_branch(st.path, br.name, br.sha)
        if res.ok:
            deleted.append((st, br))
            report.restore.append(f'git -C "{st.path}" branch {br.name} {br.sha}')
        else:
            report.failed.append(f"{st.name} {br.name}: {res.message}")
    report.branches = len(deleted)
    for st, remote in orphaned_remotes(deleted):
        res = await git.remove_remote(st.path, remote.name)
        if res.ok:
            report.remotes += 1
            report.restore.append(f'git -C "{st.path}" remote add {remote.name} {remote.url}')
        else:
            report.failed.append(f"{st.name} remote {remote.name}: {res.message}")
    return report
