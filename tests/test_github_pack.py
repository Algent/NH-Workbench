from pathlib import Path

from gtnh_devenv_tools import git, github, ops, pack, render
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


def test_parse_reviews_marks_direct_and_sorts():
    def pr(number: int, updated: str, login: str = "someone") -> dict:
        extra = {"updatedAt": updated, "repository": {"nameWithOwner": "GTNewHorizons/Mod"}, "author": {"login": login}}
        return node(number, **extra)

    data = {
        "direct": {"nodes": [pr(2, "2026-11-01T10:00:00Z")]},
        "team": {
            "nodes": [pr(1, "2026-11-10T10:00:00Z"), pr(2, "2026-11-01T10:00:00Z"), None, pr(3, "2026-11-15T10:00:00Z")]
        },
    }
    items = github.parse_reviews(data)
    assert [(i.pr.number, i.direct) for i in items] == [(2, True), (3, False), (1, False)]
    assert items[0].repo == "GTNewHorizons/Mod" and items[0].author == "someone" and items[0].updated.year == 2026
    assert github.parse_reviews({}) == []
    query = github._review_query("review-requested:@me", after="abc")
    assert "review-requested:@me" in query and 'first: 50, after: "abc"' in query
    assert "after" not in github._review_query("user-review-requested:@me")


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

    safe, kept = await ops.cleanup_candidates([st])
    assert sorted(br.name for _, br in safe) == ["behind-pr", "pushed"]
    assert [(br.name, extra) for _, br, extra in kept] == [("extra-work", 1)]


async def test_cleanup_fetches_pr_head_missing_locally(sandbox):
    # The PR got one more commit after the last local fetch, then its branch was deleted on GitHub.
    sandbox.git(sandbox.work, "branch", "stale")
    sandbox.git(sandbox.seed, "checkout", "-b", "pr")
    pr_head = sandbox.commit(sandbox.seed, "review-fixup")
    sandbox.git(sandbox.seed, "push", "origin", "HEAD:refs/pull/7/head")

    st = await ops.scan_repo(sandbox.cfg, sandbox.work)
    st.branch("stale").pr = PrInfo(7, "MERGED", False, None, None, pr_head, "", "")
    assert not await git.has_commit(sandbox.work, pr_head)
    safe, kept = await ops.cleanup_candidates([st])
    assert [br.name for _, br in safe] == ["stale"] and kept == []

    st.branch("stale").pr = PrInfo(8, "MERGED", False, None, None, "0" * 40, "", "")
    safe, kept = await ops.cleanup_candidates([st])
    assert safe == [] and [(br.name, extra) for _, br, extra in kept] == [("stale", None)]


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


def test_remote_key_is_forge_agnostic():
    github_url = "https://github.com/GTNewHorizons/GT5-Unofficial.git"
    assert git.remote_key(github_url) == "github.com/gtnewhorizons/gt5-unofficial"
    assert git.remote_key("git@gitlab.com:CDAGaming/UniLib.git") == "gitlab.com/cdagaming/unilib"
    assert git.remote_key("ssh://git@gitlab.com/CDAGaming/UniLib") == "gitlab.com/cdagaming/unilib"
    assert git.remote_key("https://gitlab.com/group/sub/project/") == "gitlab.com/group/sub/project"
    assert git.remote_key(None) is None


def test_gitlab_source_matches_local_checkout():
    manifest = {"external_mods": {"UniLib": {"version": "1.2.1"}}}
    sources = {"unilib": {"repo": "CDAGaming/UniLib", "host": "gitlab.com", "branch": "1.7.10", "status": "active"}}
    (entry,) = pack.build_entries(manifest, {}, [], sources)
    assert (entry.key, entry.web_url) == ("gitlab.com/cdagaming/unilib", "https://gitlab.com/CDAGaming/UniLib")
    assert (entry.dev_status, entry.status) == ("active", "missing")

    local = repo("UniLib", origin="https://gitlab.com/CDAGaming/UniLib.git")
    same_name_on_github = repo("Other", origin="https://github.com/CDAGaming/UniLib.git")
    pack.link_local([entry], [same_name_on_github, local])
    assert entry.local is local and local.daily == "1.2.1" and not same_name_on_github.in_pack
    assert "gitlab.com/CDAGaming/UniLib @ 1.7.10" in render.pack_row(entry)[-1].plain


def test_fork_matches_through_its_upstream_remote():
    manifest = {"external_mods": {"Tinkers-Gregworks": {"version": "1.0.34"}}}
    sources = {"tinkers-gregworks": {"repo": "Vexatos/TinkersGregworks", "branch": "GT-NH"}}
    (entry,) = pack.build_entries(manifest, {}, [], sources)

    fork = repo(
        "TinkersGregworks",
        origin="https://github.com/GTNewHorizons/TinkersGregworks",
        UPSTREAM="https://github.com/Vexatos/TinkersGregworks",
    )
    pack.link_local([entry], [fork])
    assert entry.local is fork and fork.daily == "1.0.34"

    upstream_clone = repo("Elsewhere", origin="https://github.com/Vexatos/TinkersGregworks.git")
    pack.link_local([entry], [fork, upstream_clone])
    assert entry.local is upstream_clone and not fork.in_pack  # a direct clone wins over a fork


async def test_fork_tracks_its_upstream_source_branch(sandbox):
    sandbox.git(sandbox.work, "remote", "add", "UPSTREAM", "https://github.com/Vexatos/TinkersGregworks")
    branches = {"github.com/vexatos/tinkersgregworks": "GT-NH"}
    st = await git.read_state(sandbox.work, slug_branches=branches)
    assert st.tracked == "GT-NH" and st.off_main
    assert (await git.read_state(sandbox.work, "master", branches)).tracked == "master"  # config override wins


def test_shipped_sources_are_well_formed():
    from gtnh_devenv_tools import config

    cfg = config.load(path=Path("does-not-exist.toml"))
    assert len(cfg.sources) >= 26
    for name, source in cfg.sources.items():
        assert name == name.lower()
        assert source.get("closed") or source.get("repo"), name
        assert set(source) <= {"repo", "host", "branch", "closed", "status", "note"}, name
        assert source.get("status", "active") in {"active", "passive", "legacy"}, name
    assert cfg.slug_branches["github.com/glitchfiend/biomesoplenty"] == "BOP-1.7.10-2.1.x"
    assert cfg.slug_branches["gitlab.com/cdagaming/craftpresence"] == "1.7.10"


def test_latest_release_skips_betas_and_dailies():
    names = ["2.8.4", "2.9.0", "2.9.0-RC-2", "2.9.0-beta-3", "2.10.0-beta-1", "daily", "experimental", "previous_daily"]
    assert pack.latest_release(names) == "2.9.0"
    assert pack.latest_release(["2.9.0", "2.10.0", "2.9.1"]) == "2.10.0"
    assert pack.latest_release(["daily"]) is None


def test_pack_header():
    from datetime import UTC, datetime, timedelta

    from gtnh_devenv_tools import render
    from gtnh_devenv_tools.models import PackInfo

    now = datetime.now(UTC)
    info = PackInfo(
        release="2.9.0",
        release_updated=now - timedelta(days=16),
        daily_config="2.10.0-nightly",
        daily_updated=now - timedelta(hours=5),
        daily_build=835,
        daily_attempt=836,
        fetched=now - timedelta(minutes=12),
    )
    text = render.pack_header(info).plain
    assert "Daily #835 2.10.0-nightly · 5h ago" in text and "#836 did not succeed" in text
    assert "Release 2.9.0 · 2w ago" in text and "checked 12m ago" in text


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
    release = {"github_mods": {"GT5-Unofficial": {"version": "5.08"}, "OldMod": {"version": "1.0"}}}
    entries = {e.name: e for e in pack.build_entries(manifest, release, assets, sources)}

    names = ("GT5-Unofficial", "Applied-Energistics-2-Unofficial", "Unrelated", "OldMod")
    gt5, ae2, renamed, old = (repo(n) for n in names)
    ae2.canonical = "GTNewHorizons/Applied-Energistics-2-NH"
    pack.link_local(entries.values(), [gt5, ae2, renamed, old])

    assert entries["GT5-Unofficial"].local is gt5 and (gt5.daily, gt5.release) == ("5.09", "5.08")
    assert (entries["GT5-Unofficial"].version, entries["GT5-Unofficial"].release_version) == ("5.09", "5.08")
    ae2_nh = entries["Applied-Energistics-2-NH"]
    assert ae2_nh.local is ae2 and ae2_nh.release_version == ""
    assert (entries["OldMod"].version, entries["OldMod"].release_version) == ("", "1.0")
    assert entries["OldMod"].local is old and (old.daily, old.release, old.in_pack) == ("", "1.0", True)
    assert not renamed.in_pack
    assert {n: e.status for n, e in entries.items() if e.kind == "external"} == {
        "UniMixins": "missing",
        "Biomes O' Plenty": "missing",
        "Thaumcraft": "closed",
        "Mystery": "unknown",
    }
    assert (entries["Biomes O' Plenty"].repo, entries["Biomes O' Plenty"].branch) == ("Example/BoP", "1.7.10")
    assert pack.find(entries.values(), "unimixins") is entries["UniMixins"]
    assert pack.find(entries.values(), "bop") is entries["Biomes O' Plenty"]
