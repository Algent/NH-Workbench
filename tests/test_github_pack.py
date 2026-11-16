from pathlib import Path

from gtnh_devenv_tools import git, github, ops, pack
from gtnh_devenv_tools.models import Branch, PrInfo, RepoState


def repo(name: str = "Backhand", **remotes: str) -> RepoState:
    remotes = remotes or {"origin": f"https://github.com/GTNewHorizons/{name}.git"}
    st = RepoState(Path(name), name, head="master", tracked="master")
    st.remotes = {n: git.parse_remote(n, url) for n, url in remotes.items()}
    return st


def node(number: int, state: str = "OPEN", sha: str = "s", owner: str = "GTNewHorizons", **extra) -> dict:
    return {
        "number": number,
        "state": state,
        "headRefOid": sha,
        "headRepositoryOwner": {"login": owner},
        "title": f"PR {number}",
        "url": f"https://example.invalid/{number}",
        **extra,
    }


def test_lookup_rules():
    st = repo(origin="https://github.com/GTNewHorizons/Backhand.git", brandyyn="https://github.com/brandyyn/Backhand")
    same = github.lookup_for(st, Branch("algent/x", "s", upstream="origin/algent/x"))
    assert (same.head, same.head_owner, same.number) == ("algent/x", "GTNewHorizons", None)
    fork = github.lookup_for(st, Branch("fork/brandyyn/master", "s", upstream="brandyyn/master"))
    assert (fork.head, fork.head_owner) == ("master", "brandyyn")
    numbered = github.lookup_for(st, Branch("algent/pr-373", "s"))
    assert numbered.number == 373
    local = github.lookup_for(st, Branch("algent/local-only", "s"))
    assert (local.head, local.head_owner) == ("algent/local-only", "GTNewHorizons")


def test_pick_prefers_matching_commit_then_open():
    lk = github.Lookup("r", "b", "mine", head="b", head_owner="GTNewHorizons")
    nodes = [node(1, "CLOSED"), node(2, "OPEN"), node(3, "MERGED", sha="mine"), node(4, owner="someone")]
    assert github.pick(nodes, lk)["number"] == 3
    assert github.pick(nodes[:2], lk)["number"] == 2
    assert github.pick([node(4, owner="someone"), None], lk) is None


def test_query_and_response_roundtrip():
    st = repo(origin="https://github.com/GTNewHorizons/Backhand.git", brandyyn="https://github.com/brandyyn/Backhand")
    st.branches = [
        Branch("master", "m", upstream="origin/master"),
        Branch("algent/x", "abc", upstream="origin/algent/x"),
        Branch("fork/brandyyn/master", "def", upstream="brandyyn/master"),
        Branch("algent/pr-7", "ghi"),
    ]
    chunks = github._chunks([st])
    query = github.build_query(chunks[0])
    assert 'repository(owner: "GTNewHorizons", name: "Backhand")' in query
    assert 'headRefName: "algent/x", first: 5' in query and 'headRefName: "master", first: 20' in query
    assert "pullRequest(number: 7)" in query and "master" not in [lk.branch for lk in chunks[0][0][1]]

    rollup = {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]}
    data = {
        "r0": {
            "nameWithOwner": "GTNewHorizons/Backhand-Renamed",
            "p0": {"nodes": [node(11, sha="abc", reviewDecision="APPROVED", commits=rollup, isDraft=True)]},
            "p1": {"nodes": [node(12, owner="other"), node(13, "MERGED", owner="brandyyn")]},
            "p2": None,
        }
    }
    result = github.PrData()
    github.parse_response(data, chunks[0], result)
    github.attach([st], result)

    assert st.canonical == "GTNewHorizons/Backhand-Renamed"
    pr = st.branch("algent/x").pr
    assert (pr.number, pr.review, pr.checks, pr.draft) == (11, "APPROVED", "FAILURE", True)
    assert st.branch("fork/brandyyn/master").pr.number == 13
    assert st.branch("algent/pr-7").pr is None


async def test_cleanup_candidates(sandbox):
    def merged(sha: str) -> PrInfo:
        return PrInfo(1, "MERGED", False, None, None, sha, "", "")

    for name in ("pushed", "behind-pr", "extra-work", "open-pr"):
        sandbox.git(sandbox.work, "branch", name)
    base = sandbox.sha(sandbox.work)
    sandbox.git(sandbox.work, "checkout", "extra-work")
    extra = sandbox.commit(sandbox.work, "never-pushed")
    sandbox.git(sandbox.work, "checkout", "master")

    st = await ops.scan_repo(sandbox.cfg, sandbox.work)
    st.branch("pushed").pr = merged(base)
    st.branch("behind-pr").pr = merged(extra)  # PR got more commits than the local branch has
    st.branch("extra-work").pr = merged(base)
    st.branch("open-pr").pr = PrInfo(2, "OPEN", False, None, None, base, "", "")
    st.branch("master").pr = merged(base)

    safe, unpushed = await ops.cleanup_candidates([st])
    assert sorted(br.name for _, br in safe) == ["behind-pr", "pushed"]
    assert [br.name for _, br in unpushed] == ["extra-work"]


async def test_cleanup_removes_fork_remote_once_unused(sandbox):
    fork = sandbox.origin.with_name("fork.git")
    sandbox.git(sandbox.origin.parent, "clone", "--bare", str(sandbox.origin), str(fork))
    sandbox.git(sandbox.work, "remote", "add", "someone", str(fork))
    sandbox.git(sandbox.work, "fetch", "someone")
    for name in ("review-a", "review-b"):
        sandbox.git(sandbox.work, "branch", "--track", name, "someone/master")
    sandbox.git(sandbox.work, "branch", "mine")

    st = await ops.scan_repo(sandbox.cfg, sandbox.work)
    a, mine = st.branch("review-a"), st.branch("mine")
    assert st.remote_of(a) == "someone" and st.remote_of(mine) is None
    assert ops.orphaned_remotes([(st, a)]) == []  # review-b still tracks it
    assert ops.orphaned_remotes([(st, mine)]) == []

    report = await ops.cleanup([(st, a)])
    assert (report.branches, report.remotes) == (1, 0)
    st = await ops.scan_repo(sandbox.cfg, sandbox.work)
    report = await ops.cleanup([(st, st.branch("review-b")), (st, st.branch("mine"))])

    assert (report.branches, report.remotes, report.failed) == (2, 1, [])
    assert sandbox.git(sandbox.work, "remote") == "origin"
    assert any("remote add someone" in line for line in report.restore)


def test_parse_github_url():
    assert pack.parse_github_url("https://github.com/Vexatos/TinkersGregworks/tree/GT-NH") == (
        "Vexatos/TinkersGregworks",
        "GT-NH",
    )
    assert pack.parse_github_url("https://github.com/LegacyModdingMC/UniMixins") == ("LegacyModdingMC/UniMixins", None)
    assert pack.parse_github_url("https://www.curseforge.com/minecraft/mc-mods/thaumcraft") == (None, None)
    assert pack.parse_github_url(None) == (None, None)


def test_pack_entries_and_local_matching():
    manifest = {
        "github_mods": {"GT5-Unofficial": {"version": "5.09"}, "Applied-Energistics-2-NH": {"version": "1.0"}},
        "external_mods": {
            "UniMixins": {"version": "0.3"},
            "Biomes O' Plenty": {"version": "2.1"},
            "Thaumcraft": {"version": "4.2"},
            "Mystery": {"version": "1"},
        },
    }
    assets = [
        {"name": "GT5-Unofficial", "repo_url": "https://github.com/GTNewHorizons/GT5-Unofficial", "private": False},
        {"name": "UniMixins", "external_url": "https://github.com/LegacyModdingMC/UniMixins", "license": "MIT"},
        {"name": "Thaumcraft", "external_url": "https://www.curseforge.com/x", "license": "ARR"},
    ]
    sources = {"biomes o' plenty": {"repo": "Example/BoP", "branch": "1.7.10"}, "thaumcraft": {"closed": True}}
    entries = {e.name: e for e in pack.build_entries(manifest, assets, sources)}

    gt5, ae2, renamed = repo("GT5-Unofficial"), repo("Applied-Energistics-2-Unofficial"), repo("Unrelated")
    ae2.canonical = "GTNewHorizons/Applied-Energistics-2-NH"
    pack.link_local(entries.values(), [gt5, ae2, renamed])

    assert entries["GT5-Unofficial"].local is gt5 and gt5.pack == "daily 5.09"
    assert entries["Applied-Energistics-2-NH"].local is ae2
    assert renamed.pack == ""
    assert {n: e.status for n, e in entries.items() if e.kind == "external"} == {
        "UniMixins": "missing",
        "Biomes O' Plenty": "missing",
        "Thaumcraft": "closed",
        "Mystery": "unknown",
    }
    assert (entries["Biomes O' Plenty"].repo, entries["Biomes O' Plenty"].branch) == ("Example/BoP", "1.7.10")
    assert pack.find(entries.values(), "unimixins") is entries["UniMixins"]
    assert pack.find(entries.values(), "bop") is entries["Biomes O' Plenty"]
