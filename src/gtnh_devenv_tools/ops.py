import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from . import git
from .config import Config
from .models import Branch, Outcome, RepoState


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


async def cleanup_candidates(states: Iterable[RepoState]) -> tuple[list[tuple[RepoState, Branch]], list[tuple]]:
    """Split merged-PR branches into safe to delete and merged-but-holding-local-commits."""
    safe, unpushed = [], []
    for st in states:
        for br in st.branches:
            if br.name in (st.head, st.tracked) or not br.pr or br.pr.state != "MERGED":
                continue
            pushed = br.sha == br.pr.head_sha or await git.is_ancestor(st.path, br.sha, br.pr.head_sha)
            (safe if pushed else unpushed).append((st, br))
    return safe, unpushed
