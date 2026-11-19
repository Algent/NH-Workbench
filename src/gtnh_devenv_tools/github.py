import asyncio
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import cache_dir
from .models import Branch, PrHead, PrInfo, Remote, RepoState, ReviewItem
from .proc import run

_PR_NUMBER = re.compile(r"(?:^|[/_-])pr[-_]?(\d+)$", re.IGNORECASE)
_FRAGMENT = (
    "fragment pr on PullRequest { number state isDraft reviewDecision title url headRefOid"
    " headRepositoryOwner { login } commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }"
)
_CHUNK = 20  # branch lookups per GraphQL request


@dataclass(frozen=True)
class Lookup:
    repo: str  # local repo name
    branch: str
    sha: str
    number: int | None = None
    head: str | None = None
    head_owner: str | None = None


@dataclass
class PrData:
    prs: dict[tuple[str, str], PrInfo] = field(default_factory=dict)
    canonical: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    rank: dict[tuple[str, str], tuple] = field(default_factory=dict)  # how well each kept PR fits its branch


def lookup_for(st: RepoState, br: Branch) -> Lookup:
    remote = st.remote_of(br)
    if remote:
        head = br.upstream[len(remote) + 1 :]
        return Lookup(st.name, br.name, br.sha, head=head, head_owner=st.remotes[remote].owner)
    m = _PR_NUMBER.search(br.name)
    if m:
        return Lookup(st.name, br.name, br.sha, number=int(m[1]))
    return Lookup(st.name, br.name, br.sha, head=br.name, head_owner=st.origin.owner if st.origin else None)


def upstream_lookups(st: RepoState, base: Remote) -> list[Lookup]:
    """Lookups for PRs opened from origin (a fork kept to push branches) against another remote's repo."""
    origin = st.origin
    lookups = []
    for br in st.extra_branches:
        remote = st.remote_of(br)
        if remote in (None, origin.name):
            head = br.upstream[len(remote) + 1 :] if remote else br.name
            lookups.append(Lookup(st.name, br.name, br.sha, head=head, head_owner=origin.owner))
    return lookups


Target = tuple[RepoState, list[Lookup], Remote]  # repo, what to look up, and the GitHub repo to look in


def build_query(repos: list[Target]) -> str:
    parts = []
    for i, (_st, lookups, base) in enumerate(repos):
        fields = ["nameWithOwner"]
        for j, lk in enumerate(lookups):
            if lk.number is not None:
                fields.append(f"p{j}: pullRequest(number: {lk.number}) {{ ...pr }}")
            else:
                # Fork PRs share head names like "master", so look further back for those.
                first = 5 if (lk.head_owner or "").lower() == (base.owner or "").lower() else 20
                fields.append(
                    f"p{j}: pullRequests(headRefName: {json.dumps(lk.head)}, first: {first},"
                    " orderBy: {field: UPDATED_AT, direction: DESC}) { nodes { ...pr } }"
                )
        owner, name = json.dumps(base.owner), json.dumps(base.repo)
        parts.append(f"r{i}: repository(owner: {owner}, name: {name}) {{ {' '.join(fields)} }}")
    return "query { " + " ".join(parts) + " } " + _FRAGMENT


def pick(nodes: list[dict | None], lk: Lookup) -> dict | None:
    def owner(node: dict) -> str:
        return ((node.get("headRepositoryOwner") or {}).get("login") or "").lower()

    cands = [n for n in nodes if n and (lk.head_owner is None or owner(n) == lk.head_owner.lower())]
    for wanted in (lambda n: n["headRefOid"] == lk.sha, lambda n: n["state"] == "OPEN"):
        match = next((n for n in cands if wanted(n)), None)
        if match:
            return match
    return cands[0] if cands else None


def to_pr(node: dict) -> PrInfo:
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") if commits else None
    return PrInfo(
        number=node["number"],
        state=node["state"],
        draft=bool(node.get("isDraft")),
        review=node.get("reviewDecision"),
        checks=rollup.get("state") if rollup else None,
        head_sha=node.get("headRefOid") or "",
        url=node.get("url") or "",
        title=node.get("title") or "",
        additions=node.get("additions") or 0,
        deletions=node.get("deletions") or 0,
        reviews=(node["reviews"] or {}).get("totalCount") if "reviews" in node else None,
    )


def parse_response(data: dict, repos: list[Target], into: PrData) -> None:
    for i, (st, lookups, base) in enumerate(repos):
        repo = data.get(f"r{i}")
        if not repo:
            continue
        primary = base is st.origin
        if primary:
            into.canonical[st.name] = repo["nameWithOwner"]
        for j, lk in enumerate(lookups):
            value = repo.get(f"p{j}")
            nodes = [value] if lk.number is not None else (value or {}).get("nodes") or []
            node = pick(nodes, lk)
            if not node:
                continue
            # A branch can have a PR in its own repo and one upstream: keep the one that fits it best.
            found = to_pr(node)
            rank = (found.head_sha == lk.sha, found.state == "OPEN", primary)
            key = (st.name, lk.branch)
            if key not in into.prs or rank > into.rank[key]:
                into.prs[key], into.rank[key] = found, rank


def _chunks(states: Iterable[RepoState]) -> list[list[Target]]:
    targets: list[Target] = []
    for st in states:
        if st.error or not st.origin or not st.origin.owner:
            continue
        targets.append((st, [lookup_for(st, br) for br in st.extra_branches], st.origin))
        for remote in st.remotes.values():
            if remote.owner and remote.slug.lower() != st.origin.slug.lower():
                lookups = upstream_lookups(st, remote)
                if lookups:
                    targets.append((st, lookups, remote))

    chunks, current, size = [], [], 0
    for target in targets:
        current.append(target)
        size += max(len(target[1]), 1)
        if size >= _CHUNK:
            chunks.append(current)
            current, size = [], 0
    if current:
        chunks.append(current)
    return chunks


async def fetch_prs(states: Iterable[RepoState]) -> PrData:
    """Resolve PRs for every non-main local branch and each repo's canonical name, through the gh CLI."""
    result = PrData()
    chunks = _chunks(states)
    answers = await asyncio.gather(*(_graphql(build_query(chunk)) for chunk in chunks))
    for chunk, (data, error) in zip(chunks, answers, strict=True):
        if data is None:
            result.error = error
        else:
            parse_response(data, chunk, result)
    return result


async def _graphql(query: str, attempts: int = 2) -> tuple[dict | None, str | None]:
    """Run a query through the gh CLI. Returns (data, None) or (None, why it failed)."""
    data, error = await _graphql_once(query)
    # Connections to GitHub drop now and then; one more try saves a whole batch of repos.
    if data is None and attempts > 1 and error != _NO_GH:
        return await _graphql(query, attempts - 1)
    return data, error


_NO_GH = "gh CLI not found"


async def _graphql_once(query: str) -> tuple[dict | None, str | None]:
    res = await run("gh", "api", "graphql", "--input", "-", stdin=json.dumps({"query": query}), timeout=30)
    if res.code == 127:
        return None, _NO_GH
    try:
        payload = json.loads(res.out)
    except ValueError:
        return None, res.message
    # gh exits non-zero on partial errors (a deleted repo, an unknown PR number) but still returns data.
    if not payload.get("data"):
        errors = payload.get("errors") or [{}]
        return None, errors[0].get("message") or payload.get("message") or res.message
    return payload["data"], None


async def pr_head(base: str, number: int) -> tuple[PrHead | None, str | None]:
    """Where the commits of base#number live, to check it out."""
    owner, _, name = base.partition("/")
    query = (
        f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
        f" pullRequest(number: {number}) {{ headRefName headRepository {{ nameWithOwner url }} }} }} }}"
    )
    data, error = await _graphql(query)
    node = ((data or {}).get("repository") or {}).get("pullRequest")
    if not node:
        return None, error or f"{base}#{number} not found"
    repo = node.get("headRepository") or {}
    url = f"{repo['url']}.git" if repo.get("url") else ""
    return PrHead(number, base, node.get("headRefName") or "", repo.get("nameWithOwner"), url), None


_SEARCH_FIELDS = (
    "number state isDraft reviewDecision title url headRefOid updatedAt additions deletions"
    " repository { nameWithOwner } author { login }"
)
_CI_FIELDS = " commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }"
# The review decision is what makes big searches time out on GitHub's side, so the org-wide one goes without.
_ORG_FIELDS = _SEARCH_FIELDS.replace(" reviewDecision", "") + " reviews { totalCount }"


def _review_query(qualifier: str, after: str | None = None, fields: str = _SEARCH_FIELDS) -> str:
    # No CI rollup by default, and small pages: the team review search times out on GitHub's side otherwise.
    cursor = f", after: {json.dumps(after)}" if after else ""
    search = f'search(query: "is:pr is:open archived:false {qualifier}", type: ISSUE, first: 50{cursor})'
    nodes = f"nodes {{ ... on PullRequest {{ {fields} }} }}"
    return f"query {{ {search} {{ pageInfo {{ hasNextPage endCursor }} {nodes} }} }}"


async def _search_all(qualifier: str, pages: int = 4, fields: str = _SEARCH_FIELDS) -> tuple[list[dict], str | None]:
    nodes: list[dict] = []
    after = None
    for _ in range(pages):
        data, error = await _graphql(_review_query(qualifier, after, fields))
        if data is None:
            return nodes, error
        nodes += data["search"].get("nodes") or []
        page = data["search"].get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
    return nodes, None


def parse_reviews(data: dict) -> list[ReviewItem]:
    """Open PRs waiting on your review: the ones asked of you personally first, then by last update."""

    def nodes(alias: str) -> list[dict]:
        return [n for n in (data.get(alias) or {}).get("nodes") or [] if n and n.get("url")]

    direct = {n["url"] for n in nodes("direct")}
    items: dict[str, ReviewItem] = {}
    for node in nodes("direct") + nodes("team"):
        updated = node.get("updatedAt")
        items.setdefault(
            node["url"],
            ReviewItem(
                repo=(node.get("repository") or {}).get("nameWithOwner", ""),
                pr=to_pr(node),
                author=(node.get("author") or {}).get("login", ""),
                updated=datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else None,
                direct=node["url"] in direct,
            ),
        )
    return sorted(_newest_first(items.values()), key=lambda i: not i.direct)


def _newest_first(items: Iterable[ReviewItem]) -> list[ReviewItem]:
    return sorted(items, key=lambda i: i.updated.timestamp() if i.updated else 0, reverse=True)


async def fetch_mine() -> tuple[list[ReviewItem], str | None]:
    """Your own open PRs across GitHub, newest first, with their CI state."""
    nodes, error = await _search_all("author:@me", fields=_SEARCH_FIELDS + _CI_FIELDS)
    if error and not nodes:
        return [], error
    return _newest_first(parse_reviews({"direct": {"nodes": nodes}})), error


async def fetch_reviews(team: bool = False) -> tuple[list[ReviewItem], str | None]:
    """Open PRs waiting on your review; with team, also the (slow) ones that reach you through a team."""
    direct, error = await _search_all("user-review-requested:@me")
    if error:
        return [], error
    if not team:
        return parse_reviews({"direct": {"nodes": direct}}), None
    # A failed team search still returns the direct requests.
    through_team, error = await _search_all("review-requested:@me")
    return parse_reviews({"direct": {"nodes": direct}, "team": {"nodes": through_team}}), error


ORG_MAX_AGE = 30 * 60  # seconds before opening the All PRs tab searches again


@dataclass
class OrgPrs:
    items: list[ReviewItem]
    reviewed: set[str] = field(default_factory=set)  # URLs of the PRs you reviewed
    fetched: float = 0.0  # time.time() of the search
    error: str | None = None


def _org_file() -> Path:
    return cache_dir() / "org-prs.json"


STALE_DAYS = 30
# Narrowing the org list down, all of them on data already loaded.
PR_FILTERS = {
    "local": "in repos checked out here",
    "pack": "in pack mods",
    "unreviewed": "no reviews yet",
    "stale": f"no update for {STALE_DAYS}+ days",
}


def filter_prs(
    items: Iterable[ReviewItem], wanted: Iterable[str], local: set[str], pack: set[str], now: datetime | None = None
) -> list[ReviewItem]:
    """Items passing every wanted filter. local and pack hold lower-case owner/name of repos.

    PRs from an older search without review counts pass the unreviewed filter.
    """
    wanted = set(wanted)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=STALE_DAYS)

    def keep(item: ReviewItem) -> bool:
        repo = item.repo.lower()
        return (
            ("local" not in wanted or repo in local)
            and ("pack" not in wanted or repo in pack)
            and ("unreviewed" not in wanted or not item.pr.reviews)
            and ("stale" not in wanted or (item.updated is not None and item.updated < cutoff))
        )

    return [i for i in items if keep(i)]


def _org_items(nodes: list[dict]) -> list[ReviewItem]:
    return _newest_first(parse_reviews({"direct": {"nodes": nodes}}))


def cached_org(org: str) -> OrgPrs | None:
    """The last org search saved on disk, if it was for this org."""
    try:
        data = json.loads(_org_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("org") != org:
        return None
    return OrgPrs(_org_items(data.get("nodes") or []), set(data.get("reviewed") or []), float(data.get("fetched") or 0))


def _created_ranges(today: date, edges: tuple[int, ...] = (7, 14, 30, 60, 120)) -> list[str]:
    """Search qualifiers splitting PRs by creation date (days ago), without gaps or overlaps."""
    days = [today - timedelta(d) for d in edges]
    ranges = [f"created:>={days[0]}"]
    ranges += [f"created:{start}..{end - timedelta(1)}" for start, end in zip(days[1:], days, strict=False)]
    return ranges + [f"created:<{days[-1]}"]


async def fetch_org(org: str) -> OrgPrs:
    """Every open PR in the org, and which of them you reviewed. Saved on disk.

    Paging through one big search takes a minute, so it runs as one search per creation-date range, in parallel.
    """
    searches = [_search_all(f"org:{org} {r}", pages=20, fields=_ORG_FIELDS) for r in _created_ranges(date.today())]
    *parts, (reviewed, _) = await asyncio.gather(
        *searches, _search_all(f"org:{org} reviewed-by:@me", pages=4, fields="url")
    )
    by_url = {n["url"]: n for found, _ in parts for n in found if n and n.get("url")}
    nodes = list(by_url.values())
    error = next((e for _, e in parts if e), None)
    urls = {n["url"] for n in reviewed if n and n.get("url")}
    result = OrgPrs(_org_items(nodes), urls, time.time(), error)
    if nodes and not error:
        try:
            path = _org_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"org": org, "fetched": result.fetched, "nodes": nodes, "reviewed": sorted(urls)}
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass  # the list still shows, it just is not kept for the next start
    return result


def attach(states: Iterable[RepoState], data: PrData) -> None:
    for st in states:
        st.canonical = data.canonical.get(st.name, st.canonical)
        for br in st.branches:
            br.pr = data.prs.get((st.name, br.name))
