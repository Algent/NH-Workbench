import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import git
from .config import Config
from .models import Branch, Outcome, PrHead, Remote, RepoState


def _subdirs(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_dir()]
    except OSError:  # missing or unreadable: nothing to list
        return []


def _is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def discover(cfg: Config) -> list[Path]:
    """Repos directly under the root, plus one level down inside plain folders (which act as groups).

    Dot folders count only when they are repos themselves (like .github); they are never searched as groups.
    """
    found = []
    for child in _subdirs(cfg.root):
        if _is_repo(child):
            found.append(child)
        elif not child.name.startswith("."):
            found += [p for p in _subdirs(child) if _is_repo(p)]
    return sorted((p for p in found if not cfg.repo(p.name).ignore), key=lambda p: p.name.lower())


def by_repo(st: RepoState) -> tuple[str, bool, str]:
    """Sort key by name that keeps linked working trees right under the repo they belong to."""
    return (st.repo_root.name.lower(), st.worktree_of is not None, st.name.lower())


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
    # Worktrees share their branches with the main checkout, so its protections apply too.
    keep = set(cfg.repo(path.name).keep_branches) | set(cfg.repo(st.repo_root.name).keep_branches)
    for br in st.branches:
        br.keep = br.name in keep
    return st


_fetch_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _fetch_lock(st: RepoState) -> asyncio.Lock:
    """Working trees of one repo share their refs, so their fetches must not overlap."""
    key = (id(asyncio.get_running_loop()), str(st.repo_root.resolve()).lower())
    return _fetch_locks.setdefault(key, asyncio.Lock())


async def fetch_repo(cfg: Config, path: Path) -> RepoState:
    st = await scan_repo(cfg, path)
    if st.error:
        return st
    async with _fetch_lock(st):
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


async def pack_progress(states: Iterable[RepoState], release_branch: str = "", limit: int = 8) -> None:
    """Measure each repo against the pack, as of the last fetch.

    daily_ahead: commits on main that the version in the daily lacks (merged, not in a daily yet).
    release_ahead / release_commits: what the release branch gained since the released version.
    release_delta: commits on main since the released version (how far a backport has to reach).
    """

    async def one(st: RepoState) -> None:
        st.daily_ahead = st.release_ahead = st.release_delta = None
        st.release_commits = []
        if not st.tracked:
            return
        main = st.branch(st.tracked)
        upstream = main.upstream if main and main.upstream and not main.gone else f"origin/{st.tracked}"
        if st.daily:
            st.daily_ahead = await git.commits_not_in(st.path, f"refs/tags/{st.daily}", f"refs/remotes/{upstream}")
        if st.release:
            st.release_delta = await git.commits_not_in(st.path, f"refs/tags/{st.release}", f"refs/remotes/{upstream}")
        if st.release and release_branch in st.release_branches:
            tip = f"refs/remotes/origin/{release_branch}"
            subjects = await git.subjects_between(st.path, f"refs/tags/{st.release}", tip)
            if subjects is not None:
                st.release_ahead, st.release_commits = len(subjects), subjects

    await run_all(states, one, limit)


def to_main_candidates(states: Iterable[RepoState]) -> list[RepoState]:
    """Repos parked on a branch whose PR is merged, with nothing uncommitted in the way."""
    return [
        st
        for st in states
        if st.off_main
        and not st.dirty
        and st.current
        and st.current.pr
        and st.current.pr.state == "MERGED"
        and not st.checked_out_elsewhere(st.tracked)
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


def local_slugs(states: Iterable[RepoState]) -> set[str]:
    """Lower-case owner/name of every GitHub repo checked out here, through any remote."""
    slugs = {(st.canonical or "").lower() for st in states}
    slugs |= {r.slug.lower() for st in states for r in st.remotes.values() if r.slug}
    return slugs - {""}


def _remote_for(st: RepoState, slug: str | None, url: str = "") -> Remote | None:
    key = git.remote_key(url) or url
    for r in st.remotes.values():
        if (slug and (r.slug or "").lower() == slug.lower()) or (url and (git.remote_key(r.url) or r.url) == key):
            return r
    if slug and st.origin and (st.canonical or "").lower() == slug.lower():
        return st.origin
    return None


async def checkout_pr(cfg: Config, st: RepoState, head: PrHead) -> RepoState:
    """Switch to a PR's branch, creating it when needed.

    A branch from origin keeps its name; one from a fork becomes PR-<n>, tracking the fork's branch through a
    remote named after its owner, so it shows behind/gone like any other and the cleanup can drop both later.
    """

    async def finish(kind: str, text: str) -> RepoState:
        new = await scan_repo(cfg, st.path)
        new.note = Outcome(kind, text)
        return new

    if st.dirty:
        return await finish("failed", "uncommitted changes, PR not checked out")
    pr_name = f"PR-{head.number}"
    if not head.slug:  # head repo deleted: GitHub still serves the commits on the base repo
        base = _remote_for(st, head.base) or st.origin
        if not st.branch(pr_name):
            res = await git.git(st.path, "fetch", "--no-tags", base.name, f"refs/pull/{head.number}/head:{pr_name}")
            if not res.ok:
                return await finish("failed", f"fetch failed: {res.message}")
        failed = await _switch_to(st, pr_name)
        return await finish("failed", failed) if failed else await finish("updated", f"on {pr_name}, fork deleted")

    remote = _remote_for(st, head.slug, head.url)
    added = None
    if not remote:
        added = head.owner if head.owner not in st.remotes else head.slug.replace("/", "-")
        res = await git.git(st.path, "remote", "add", added, head.url)
        if not res.ok:
            return await finish("failed", f"could not add remote {added}: {res.message}")
        remote = git.parse_remote(added, head.url)
    upstream = f"{remote.name}/{head.ref}"
    res = await git.git(
        st.path, "fetch", "--no-tags", remote.name, f"+refs/heads/{head.ref}:refs/remotes/{upstream}", timeout=600
    )
    if not res.ok:
        if added:
            await git.remove_remote(st.path, added)
        return await finish("failed", f"fetch failed: {res.message}")
    note = f", new remote {added}" if added else ""

    existing = next((b for b in st.branches if b.upstream == upstream), None)
    if existing:
        if failed := await _switch_to(st, existing.name):
            return await finish("failed", failed)
        await git.git(st.path, "merge", "--ff-only", "-q", upstream)  # fine if diverged: shown as such
        return await finish("updated", f"on {existing.name}{note}")
    name = head.ref if st.origin and remote.name == st.origin.name and not st.branch(head.ref) else pr_name
    if st.branch(name):
        return await finish("failed", f"branch {name} already exists, tracking {st.branch(name).upstream or 'nothing'}")
    res = await git.git(st.path, "switch", "--no-track", "-c", name, upstream, timeout=300)
    if not res.ok:
        return await finish("failed", f"switch failed: {res.message}")
    await git.git(st.path, "config", f"branch.{name}.remote", remote.name)
    await git.git(st.path, "config", f"branch.{name}.merge", f"refs/heads/{head.ref}")
    return await finish("updated", f"on {name}, tracking {upstream}{note}")


async def _switch_to(st: RepoState, name: str) -> str | None:
    """Check out an existing branch. Returns why it failed, or None."""
    if elsewhere := st.checked_out_elsewhere(name):
        return f"{name} is checked out in {elsewhere.path}"
    if st.head != name and not (res := await git.switch(st.path, name)).ok:
        return f"switch failed: {res.message}"
    return None


async def cleanup_candidates(
    states: Iterable[RepoState], parked: bool = False
) -> tuple[list[tuple[RepoState, Branch]], list[tuple]]:
    """Split merged-PR branches into safe to delete and kept ones.

    With parked, the checked-out branch counts too when the repo can switch back to main first (see to_main).
    Kept entries are (repo, branch, n): n local commits are not in the PR, or None if that could not be checked.
    """
    states = list(states)
    switchable = {st.path for st in to_main_candidates(states)} if parked else set()
    safe, kept = [], []
    for st in states:
        for br in st.branches:
            pr = br.pr
            current = br.name == st.head and st.path not in switchable
            in_use = current or br.name == st.tracked or st.checked_out_elsewhere(br.name)
            if in_use or br.keep or not pr or pr.state != "MERGED":
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
