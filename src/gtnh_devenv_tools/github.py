import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import Branch, PrInfo, RepoState
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


def lookup_for(st: RepoState, br: Branch) -> Lookup:
    remote = st.remote_of(br)
    if remote:
        head = br.upstream[len(remote) + 1 :]
        return Lookup(st.name, br.name, br.sha, head=head, head_owner=st.remotes[remote].owner)
    m = _PR_NUMBER.search(br.name)
    if m:
        return Lookup(st.name, br.name, br.sha, number=int(m[1]))
    return Lookup(st.name, br.name, br.sha, head=br.name, head_owner=st.origin.owner if st.origin else None)


def build_query(repos: list[tuple[RepoState, list[Lookup]]]) -> str:
    parts = []
    for i, (st, lookups) in enumerate(repos):
        base = st.origin
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
    )


def parse_response(data: dict, repos: list[tuple[RepoState, list[Lookup]]], into: PrData) -> None:
    for i, (st, lookups) in enumerate(repos):
        repo = data.get(f"r{i}")
        if not repo:
            continue
        into.canonical[st.name] = repo["nameWithOwner"]
        for j, lk in enumerate(lookups):
            value = repo.get(f"p{j}")
            nodes = [value] if lk.number is not None else (value or {}).get("nodes") or []
            node = pick(nodes, lk)
            if node:
                into.prs[(st.name, lk.branch)] = to_pr(node)


def _chunks(states: Iterable[RepoState]) -> list[list[tuple[RepoState, list[Lookup]]]]:
    chunks, current, size = [], [], 0
    for st in states:
        if st.error or not st.origin or not st.origin.owner:
            continue
        lookups = [lookup_for(st, br) for br in st.extra_branches]
        current.append((st, lookups))
        size += max(len(lookups), 1)
        if size >= _CHUNK:
            chunks.append(current)
            current, size = [], 0
    if current:
        chunks.append(current)
    return chunks


async def fetch_prs(states: Iterable[RepoState]) -> PrData:
    """Resolve PRs for every non-main local branch and each repo's canonical name, through the gh CLI."""
    result = PrData()
    for chunk in _chunks(states):
        body = json.dumps({"query": build_query(chunk)})
        res = await run("gh", "api", "graphql", "--input", "-", stdin=body, timeout=60)
        if res.code == 127:
            result.error = "gh CLI not found"
            break
        try:
            payload = json.loads(res.out)
        except ValueError:
            result.error = res.message
            break
        # gh exits non-zero on partial errors (a deleted repo, an unknown PR number) but still returns data.
        if not payload.get("data"):
            errors = payload.get("errors") or [{}]
            result.error = errors[0].get("message") or payload.get("message") or res.message
            break
        parse_response(payload["data"], chunk, result)
    return result


def attach(states: Iterable[RepoState], data: PrData) -> None:
    for st in states:
        st.canonical = data.canonical.get(st.name, st.canonical)
        for br in st.branches:
            br.pr = data.prs.get((st.name, br.name))
