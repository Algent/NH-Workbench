from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Remote:
    name: str
    url: str
    owner: str | None = None
    repo: str | None = None

    @property
    def slug(self) -> str | None:
        return f"{self.owner}/{self.repo}" if self.owner else None


@dataclass(frozen=True)
class PrInfo:
    number: int
    state: str  # OPEN | MERGED | CLOSED
    draft: bool
    review: str | None
    checks: str | None
    head_sha: str
    url: str
    title: str


@dataclass(frozen=True)
class ReviewItem:
    repo: str  # owner/name
    pr: PrInfo
    author: str
    updated: datetime | None
    direct: bool  # asked of you personally, not only through a team


@dataclass(frozen=True)
class Outcome:
    kind: str  # updated | current | skipped | failed | planned | info
    text: str
    commits: int = 0


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None  # None when detached


@dataclass
class Branch:
    name: str
    sha: str
    upstream: str | None = None
    gone: bool = False
    ahead: int = 0
    behind: int = 0
    date: datetime | None = None
    subject: str = ""
    pr: PrInfo | None = None


@dataclass
class RepoState:
    path: Path
    name: str
    head: str | None = None  # None when detached
    head_sha: str = ""
    tracked: str | None = None  # the branch that counts as "main" for this repo
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    changed: int = 0
    untracked: int = 0
    stash: int = 0
    in_progress: str | None = None
    remotes: dict[str, Remote] = field(default_factory=dict)
    branches: list[Branch] = field(default_factory=list)
    date: datetime | None = None
    subject: str = ""
    error: str | None = None
    note: Outcome | None = None
    canonical: str | None = None  # owner/name as GitHub reports it (follows renames)
    group: str = ""  # gtnh | other | name of the subfolder or the config override
    daily: str = ""  # version the current daily ships, empty if it does not ship this repo
    release: str = ""  # version the latest stable release shipped
    release_branches: list[str] = field(default_factory=list)  # e.g. release/2.9.x, as found on origin
    release_ahead: int | None = None  # commits on the release branch since the released version
    release_commits: list[str] = field(default_factory=list)  # their subjects, newest first
    daily_ahead: int | None = None  # commits on main that the daily's version does not have yet
    worktrees: list[Worktree] = field(default_factory=list)  # the repo's other working trees
    worktree_of: Path | None = None  # main working tree, when this folder is a linked one

    @property
    def repo_root(self) -> Path:
        """Folder of the main working tree; all worktrees of one repo share it (and its refs)."""
        return self.worktree_of or self.path

    def checked_out_elsewhere(self, branch: str | None) -> Worktree | None:
        return next((w for w in self.worktrees if branch and w.branch == branch), None)

    @property
    def in_pack(self) -> bool:
        return bool(self.daily or self.release)

    @property
    def origin(self) -> Remote | None:
        return self.remotes.get("origin") or next(iter(self.remotes.values()), None)

    @property
    def slug(self) -> str | None:
        return self.canonical or (self.origin.slug if self.origin else None)

    def branch(self, name: str | None) -> Branch | None:
        return next((b for b in self.branches if b.name == name), None)

    def remote_of(self, br: Branch) -> str | None:
        """Name of the remote a branch tracks. Remote names may contain slashes, so the longest match wins."""
        names = sorted(self.remotes, key=len, reverse=True)
        return next((n for n in names if (br.upstream or "").startswith(n + "/")), None)

    @property
    def current(self) -> Branch | None:
        return self.branch(self.head)

    @property
    def off_main(self) -> bool:
        return self.tracked is not None and self.head != self.tracked

    @property
    def gone(self) -> bool:
        cur = self.current
        return bool(cur and cur.gone)

    @property
    def main_behind(self) -> int:
        """How far the local main branch lags when another branch is checked out."""
        main = self.branch(self.tracked) if self.off_main else None
        return main.behind if main else 0

    @property
    def is_behind(self) -> bool:
        return self.behind > 0 or self.main_behind > 0

    @property
    def dirty(self) -> bool:
        return self.changed > 0 or self.in_progress is not None

    @property
    def extra_branches(self) -> list[Branch]:
        return [b for b in self.branches if b.name != self.tracked]


@dataclass
class PackEntry:
    name: str
    kind: str  # github | external
    version: str  # in the current daily, empty when the daily no longer ships it
    release_version: str = ""  # in the latest stable release, empty when it was not part of it
    repo: str | None = None  # owner/name of the source repository, when known
    host: str = "github.com"
    branch: str | None = None
    dev_status: str = ""  # active | passive | legacy, for sources maintained outside the org
    note: str = ""
    jar_name: str = ""  # file DreamAssemblerXXL ships for the version in use, for mods without source
    jar_url: str = ""
    closed: bool = False
    private: bool = False
    license: str | None = None
    url: str | None = None
    local: RepoState | None = None

    @property
    def key(self) -> str | None:
        """host/owner/name in lower case, comparable with git.remote_key()."""
        return f"{self.host}/{self.repo}".lower() if self.repo else None

    @property
    def web_url(self) -> str | None:
        return f"https://{self.host}/{self.repo}" if self.repo else self.url

    @property
    def status(self) -> str:
        if self.local:
            return "cloned"
        if self.closed:
            return "closed"
        if not self.repo:
            return "unknown"
        return "missing"


@dataclass
class PackInfo:
    entries: list[PackEntry] = field(default_factory=list)
    release: str = ""  # latest stable release, e.g. 2.9.0
    release_updated: datetime | None = None
    daily_config: str = ""
    daily_updated: datetime | None = None
    daily_build: int | None = None  # latest daily that built successfully
    daily_attempt: int | None = None  # latest daily that was started
    fetched: datetime | None = None
