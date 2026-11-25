import asyncio
import re
from datetime import datetime
from pathlib import Path

from .models import Branch, Outcome, Remote, RepoState, Worktree
from .proc import Result, run

_GITHUB = re.compile(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$", re.IGNORECASE)
_REMOTE = re.compile(r"^(?:\w+://)?(?:[^@/]+@)?([^/:]+)[:/]+(.+?)(?:\.git)?/?$")
_TRACK = re.compile(r"(ahead|behind) (\d+)")
_REF_FORMAT = "%00".join(
    [
        "%(refname)",
        "%(objectname)",
        "%(upstream:short)",
        "%(upstream:track,nobracket)",
        "%(committerdate:iso-strict)",
        "%(symref:short)",
        "%(contents:subject)",
    ]
)
_RELEASE_REFS = "refs/remotes/origin/release"
_IN_PROGRESS = {
    "rebase-merge": "rebase",
    "rebase-apply": "rebase",
    "MERGE_HEAD": "merge",
    "CHERRY_PICK_HEAD": "cherry-pick",
    "REVERT_HEAD": "revert",
    "BISECT_LOG": "bisect",
}


async def git(repo: Path, *args: str, timeout: float = 120) -> Result:
    return await run("git", "-C", str(repo), *args, timeout=timeout)


def remote_key(url: str | None) -> str | None:
    """host/owner/name in lower case for any https or ssh remote URL, whatever the forge."""
    m = _REMOTE.search(url or "")
    return f"{m[1]}/{m[2]}".lower() if m else None


def parse_remote(name: str, url: str) -> Remote:
    m = _GITHUB.search(url)
    return Remote(name, url, m[1], m[2]) if m else Remote(name, url)


def parse_status(text: str, st: RepoState) -> None:
    """Fill st from `git status --porcelain=v2 --branch --show-stash` output."""
    for line in text.splitlines():
        if line.startswith("# branch.oid "):
            oid = line[13:]
            st.head_sha = "" if oid == "(initial)" else oid
        elif line.startswith("# branch.head "):
            head = line[14:]
            st.head = None if head == "(detached)" else head
        elif line.startswith("# branch.upstream "):
            st.upstream = line[18:]
        elif line.startswith("# branch.ab "):
            ahead, behind = line[12:].split()
            st.ahead, st.behind = int(ahead), abs(int(behind))
        elif line.startswith("# stash "):
            st.stash = int(line[8:])
        elif line[:2] in ("1 ", "2 ", "u "):
            st.changed += 1
        elif line.startswith("? "):
            st.untracked += 1


def parse_refs(text: str) -> tuple[list[Branch], str | None, list[str]]:
    """Parse for-each-ref output into local branches, origin's default branch and origin's release branches."""
    branches: list[Branch] = []
    releases: list[str] = []
    origin_head = None
    for line in text.splitlines():
        parts = line.split("\x00", 6)
        if len(parts) < 7:
            continue
        ref, sha, upstream, track, date, symref, subject = parts
        if ref == "refs/remotes/origin/HEAD":
            origin_head = symref.removeprefix("origin/") or None
            continue
        if ref.startswith("refs/remotes/origin/release/"):
            releases.append(ref.removeprefix("refs/remotes/origin/"))
            continue
        if not ref.startswith("refs/heads/"):
            continue
        counts = {kind: int(n) for kind, n in _TRACK.findall(track)}
        branches.append(
            Branch(
                name=ref.removeprefix("refs/heads/"),
                sha=sha,
                upstream=upstream or None,
                gone=track == "gone",
                ahead=counts.get("ahead", 0),
                behind=counts.get("behind", 0),
                date=datetime.fromisoformat(date) if date else None,
                subject=subject,
            )
        )
    return branches, origin_head, releases


def parse_worktrees(text: str, own: Path) -> tuple[list[Worktree], Path | None]:
    """Parse `git worktree list --porcelain` into the other working trees and, for a linked one, its main."""
    trees: list[Worktree] = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.partition(" ")[::2] for line in block.splitlines())
        # "prunable" marks a leftover entry whose folder is gone.
        if "worktree" in fields and "bare" not in fields and "prunable" not in fields:
            branch = fields.get("branch", "").removeprefix("refs/heads/") or None
            trees.append(Worktree(Path(fields["worktree"]), branch))
    if not trees:
        return [], None
    here = own.resolve()
    others = [t for t in trees if t.path.resolve() != here]
    main = trees[0].path
    return others, (main if main.resolve() != here else None)


def _git_dir(path: Path) -> Path:
    dot = path / ".git"
    if dot.is_file():
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            return (path / text[7:].strip()).resolve()
    return dot


def _in_progress(path: Path) -> str | None:
    git_dir = _git_dir(path)
    return next((label for marker, label in _IN_PROGRESS.items() if (git_dir / marker).exists()), None)


async def read_state(
    path: Path, tracked_override: str | None = None, slug_branches: dict[str, str] | None = None
) -> RepoState:
    st = RepoState(path=path, name=path.name)
    status, refs, urls, trees = await asyncio.gather(
        git(path, "status", "--porcelain=v2", "--branch", "--show-stash"),
        git(path, "for-each-ref", f"--format={_REF_FORMAT}", "refs/heads", "refs/remotes/origin/HEAD", _RELEASE_REFS),
        git(path, "config", "--get-regexp", r"^remote\..*\.url$"),
        git(path, "worktree", "list", "--porcelain"),
    )
    if not status.ok or not refs.ok:
        st.error = (status if not status.ok else refs).message
        return st

    parse_status(status.out, st)
    st.branches, origin_head, st.release_branches = parse_refs(refs.out)
    if trees.ok:
        st.worktrees, st.worktree_of = parse_worktrees(trees.out, path)
    for line in urls.out.splitlines():
        key, _, url = line.partition(" ")
        name = key.removeprefix("remote.").removesuffix(".url")
        st.remotes[name] = parse_remote(name, url.strip())
    st.in_progress = _in_progress(path)

    names = {b.name for b in st.branches}
    # A fork kept only to push branches still follows its upstream's branch, so look at every remote.
    remotes = sorted(st.remotes.values(), key=lambda r: r.name != "origin")
    known = slug_branches or {}
    st.tracked = (
        tracked_override
        or next((known[k] for k in (remote_key(r.url) for r in remotes) if k in known), None)
        or origin_head
        or next((n for n in ("main", "master") if n in names), None)
    )

    cur = st.current
    if cur:
        st.date, st.subject = cur.date, cur.subject
    elif st.head_sha:
        last = await git(path, "log", "-1", "--format=%cI%x00%s")
        if last.ok and "\x00" in last.out:
            date, subject = last.out.strip().split("\x00", 1)
            st.date, st.subject = datetime.fromisoformat(date), subject
    return st


async def fetch(st: RepoState, prune: bool = True) -> Outcome:
    if not st.remotes:
        return Outcome("skipped", "no remote")
    primary = "origin" if "origin" in st.remotes else next(iter(st.remotes))
    flags = ["--prune"] if prune else []
    res = await git(st.path, "fetch", *flags, primary, timeout=600)
    if not res.ok:
        return Outcome("failed", f"fetch failed: {res.message}")
    # Fork remotes come and go; a dead one must not hide a good origin fetch.
    warnings = []
    for name in st.remotes:
        if name != primary:
            extra = await git(st.path, "fetch", *flags, name, timeout=600)
            if not extra.ok:
                warnings.append(f"{name}: {extra.message}")
    return Outcome("info", "; ".join(warnings) if warnings else "fetched")


async def safe_pull(st: RepoState, dry_run: bool = False) -> Outcome:
    """Fast-forward the tracked branch, whether or not it is checked out. Never forces anything."""
    t = st.tracked
    if not t:
        return Outcome("skipped", "no main branch found")
    local = st.branch(t)
    if local is None:
        return Outcome("current", f"no local {t}")
    on_tracked = st.head == t
    label = "" if on_tracked else f"{t} "
    elsewhere = st.checked_out_elsewhere(t)
    if elsewhere and not on_tracked:
        # Git would refuse anyway; that working tree gets fast-forwarded on its own turn.
        return Outcome("current", f"{t} lives in {elsewhere.path.name}")
    up = local.upstream or f"origin/{t}"
    up_ref = f"refs/remotes/{up}"
    if local.gone or not (await git(st.path, "rev-parse", "-q", "--verify", up_ref)).ok:
        return Outcome("skipped", f"{label}{up} not found")

    counts = await git(st.path, "rev-list", "--left-right", "--count", f"refs/heads/{t}...{up_ref}")
    if not counts.ok:
        return Outcome("failed", f"{label}{counts.message}")
    ahead, behind = (int(n) for n in counts.out.split())
    if ahead:
        return Outcome("skipped", f"{label}diverged" if behind else f"{label}ahead {ahead}")
    if not behind:
        return Outcome("current", f"{label}up to date")
    if on_tracked and st.in_progress:
        return Outcome("skipped", f"{st.in_progress} in progress")
    if on_tracked and st.changed:
        return Outcome("skipped", f"dirty ({st.changed} changed)")
    if dry_run:
        return Outcome("planned", f"{label}+{behind}", behind)

    if on_tracked:
        res = await git(st.path, "merge", "--ff-only", up_ref, timeout=300)
    else:
        # Local fetch: git itself refuses non-fast-forwards and checked-out branches.
        refspec = f"{up_ref}:refs/heads/{t}"
        res = await git(st.path, "fetch", "--no-tags", "--no-prune", "--no-write-fetch-head", ".", refspec)
    if not res.ok:
        return Outcome("failed", f"{label}{res.message}")
    return Outcome("updated", f"{label}+{behind}", behind)


async def switch(path: Path, branch: str) -> Result:
    return await git(path, "switch", branch, timeout=300)


async def has_commit(path: Path, sha: str) -> bool:
    return (await git(path, "cat-file", "-e", f"{sha}^{{commit}}")).ok


async def fetch_pr_head(path: Path, remote: str, number: int) -> bool:
    """Download a PR's final commit; GitHub keeps refs/pull/N/head after the branch is deleted."""
    res = await git(path, "fetch", "--no-tags", "--no-write-fetch-head", remote, f"refs/pull/{number}/head")
    return res.ok


async def commits_not_in(path: Path, base: str, tip: str) -> int | None:
    """Number of commits reachable from tip but not from base, None when either is unknown locally."""
    res = await git(path, "rev-list", "--count", f"{base}..{tip}")
    return int(res.out) if res.ok else None


async def subjects_between(path: Path, base: str, tip: str) -> list[str] | None:
    """Subjects of the commits on tip that base lacks, newest first; None when either is unknown locally."""
    res = await git(path, "log", "--format=%s", f"{base}..{tip}")
    return res.out.splitlines() if res.ok else None


async def delete_branch(path: Path, name: str, expected_sha: str) -> Result:
    current = await git(path, "rev-parse", "-q", "--verify", f"refs/heads/{name}")
    if current.out.strip() != expected_sha:
        return Result(1, "", "branch moved since it was scanned")
    return await git(path, "branch", "-D", name)


async def remove_remote(path: Path, name: str) -> Result:
    return await git(path, "remote", "remove", name)


def clone_url(host: str, slug: str, protocol: str = "ssh") -> str:
    return f"git@{host}:{slug}.git" if protocol == "ssh" else f"https://{host}/{slug}.git"


_SSH_REFUSED = ("Permission denied (publickey", "Host key verification failed", "Could not resolve hostname")


def clone_error(res: Result) -> str:
    """Why a clone failed, in one line; SSH refusals get a pointer to the HTTPS setting."""
    if any(s in res.err for s in _SSH_REFUSED):
        return "SSH refused (no key for GitHub, or unknown host key). Settings (x) can switch cloning to HTTPS"
    return res.message


async def clone(url: str, dest: Path, branch: str | None = None) -> Result:
    args = ["--branch", branch] if branch else []
    return await run("git", "clone", *args, url, str(dest), timeout=3600)
