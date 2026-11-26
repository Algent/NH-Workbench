from pathlib import Path

from nh_workbench import git, github, ops, pack, render
from nh_workbench.config import RepoConfig
from nh_workbench.models import Branch, PrInfo, RepoState


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

    sandbox.cfg.repos["repo"] = RepoConfig(keep_branches=("pushed",))
    st2 = await ops.scan_repo(sandbox.cfg, sandbox.work)
    for br in st2.branches:
        br.pr = st.branch(br.name).pr
    safe, _ = await ops.cleanup_candidates([st2])
    assert [br.name for _, br in safe] == ["behind-pr"] and st2.branch("pushed").keep


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
    from nh_workbench import config

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

    from nh_workbench import render
    from nh_workbench.models import PackInfo

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
            "CoFH Core": {"version": "3.1"},
        },
    }
    assets = [
        {"name": "GT5-Unofficial", "repo_url": "https://github.com/GTNewHorizons/GT5-Unofficial", "private": False},
        {"name": "UniMixins", "external_url": "https://github.com/LegacyModdingMC/UniMixins", "license": "MIT"},
        {"name": "Thaumcraft", "external_url": "https://www.curseforge.com/x", "license": "ARR"},
        {
            "name": "CoFH Core",
            "external_url": "https://www.curseforge.com/y",
            "repo_url": "https://github.com/CoFH/CoFHCore-1.12-Legacy/tree/1.7.10",
        },
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
        "CoFH Core": "missing",
    }
    assert (entries["Biomes O' Plenty"].repo, entries["Biomes O' Plenty"].branch) == ("Example/BoP", "1.7.10")
    assert (entries["CoFH Core"].repo, entries["CoFH Core"].branch) == ("CoFH/CoFHCore-1.12-Legacy", "1.7.10")
    assert pack.find(entries.values(), "unimixins") is entries["UniMixins"]
    assert pack.find(entries.values(), "bop") is entries["Biomes O' Plenty"]


def test_prs_opened_from_a_fork_are_found_upstream():
    st = repo(
        "TinkersGregworks",
        origin="https://github.com/GTNewHorizons/TinkersGregworks",
        UPSTREAM="https://github.com/Vexatos/TinkersGregworks",
    )
    st.branches = [
        Branch("master", "m", upstream="origin/master"),
        Branch("algent/part-swap", "abc", upstream="origin/algent/part-swap"),
        Branch("algent/both", "def", upstream="origin/algent/both"),
        Branch("algent/local", "ghi"),
        Branch("GT-NH", "jkl", upstream="UPSTREAM/GT-NH"),
    ]
    (chunk,) = github._chunks([st])
    assert [(t[2].slug, len(t[1])) for t in chunk] == [
        ("GTNewHorizons/TinkersGregworks", 4),
        ("Vexatos/TinkersGregworks", 3),  # not GT-NH: that one tracks the upstream itself
    ]
    query = github.build_query(chunk)
    assert 'repository(owner: "Vexatos", name: "TinkersGregworks")' in query
    assert all(lk.head_owner == "GTNewHorizons" for lk in chunk[1][1])

    data = {
        "r0": {
            "nameWithOwner": "GTNewHorizons/TinkersGregworks",
            "p1": {"nodes": [node(26, "CLOSED", sha="old")]},  # algent/both, closed in the fork
        },
        "r1": {
            "nameWithOwner": "Vexatos/TinkersGregworks",
            "p0": {"nodes": [node(40, "OPEN", sha="abc"), node(41, "OPEN", owner="someone-else")]},
            "p1": {"nodes": [node(42, "OPEN", sha="def")]},  # algent/both, open upstream
        },
    }
    result = github.PrData()
    github.parse_response(data, chunk, result)
    github.attach([st], result)
    assert st.canonical == "GTNewHorizons/TinkersGregworks"  # never renamed after the upstream
    assert st.branch("algent/part-swap").pr.number == 40
    assert st.branch("algent/both").pr.number == 42  # the open upstream PR beats the closed fork one
    assert st.branch("algent/local").pr is None

    reversed_result = github.PrData()
    github.parse_response({"r0": data["r1"], "r1": data["r0"]}, [chunk[1], chunk[0]], reversed_result)
    assert reversed_result.prs[("TinkersGregworks", "algent/both")].number == 42  # order does not matter


async def test_graphql_retries_once_but_not_without_gh(monkeypatch):
    from nh_workbench.proc import Result

    answers = [Result(1, "", "TLS handshake timeout"), Result(0, '{"data": {"ok": 1}}', "")]

    async def flaky(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr(github, "run", flaky)
    assert await github._graphql("query { x }") == ({"ok": 1}, None) and answers == []

    calls = []

    async def missing(*args, **kwargs):
        calls.append(args)
        return Result(127, "", "not found")

    monkeypatch.setattr(github, "run", missing)
    assert await github._graphql("query { x }") == (None, "gh CLI not found") and len(calls) == 1


async def test_closed_source_jars_are_downloaded_once(tmp_path):
    jar = tmp_path / "served" / "Thaumcraft-1.7.10-4.2.3.5.jar"
    jar.parent.mkdir()
    jar.write_bytes(b"jar bytes")
    manifest = {"external_mods": {"Thaumcraft": {"version": "4.2.3.5"}, "Witchery": {"version": "0.24.1"}}}
    release = {"external_mods": {"Open": {"version": "1"}}}
    assets = [
        {
            "name": "Thaumcraft",
            "versions": [
                {"version_tag": "4.2.3.4", "filename": "old.jar", "download_url": "file:///nowhere/old.jar"},
                {"version_tag": "4.2.3.5", "filename": jar.name, "download_url": jar.as_uri()},
            ],
        },
        {"name": "Witchery", "versions": [{"version_tag": "0.24.0", "filename": "w.jar", "download_url": "x"}]},
        {"name": "Open", "versions": [{"version_tag": "1", "filename": "open.jar", "download_url": jar.as_uri()}]},
    ]
    sources = {"thaumcraft": {"closed": True}, "witchery": {"closed": True}}
    entries = pack.build_entries(manifest, release, assets, sources)
    by_name = {e.name: e for e in entries}
    assert by_name["Thaumcraft"].jar_name == jar.name and by_name["Witchery"].jar_url == ""

    folder = tmp_path / "root" / pack.JAR_FOLDER
    report = await pack.download_closed(entries, folder)
    assert report.downloaded == [jar.name] and report.present == 0
    assert report.failed == ["Witchery: no download known for this version"]
    assert (folder / jar.name).read_bytes() == b"jar bytes" and not (folder / "open.jar").exists()

    (folder / jar.name).write_bytes(b"kept")
    report = await pack.download_closed(entries, folder)
    assert (report.downloaded, report.present) == ([], 1) and (folder / jar.name).read_bytes() == b"kept"
    assert list(folder.glob("*.part")) == []


def test_slim_assets_keeps_jar_locations_for_external_mods_only():
    version = {"version_tag": "1", "filename": "a.jar", "download_url": "u", "changelog": "long text"}
    data = {"mods": [{"name": "Ext", "source": "curse", "versions": [version]}, {"name": "Org", "versions": [version]}]}
    ext, org = pack._slim_assets(data)["mods"]
    assert ext["versions"] == [{"version_tag": "1", "filename": "a.jar", "download_url": "u"}]
    assert "versions" not in org


async def test_fetch_mine_asks_for_ci_and_sorts_newest_first(monkeypatch):
    queries = []

    async def fake_graphql(query, attempts=2):
        queries.append(query)
        nodes = [
            node(1, updatedAt="2026-11-01T10:00:00Z", repository={"nameWithOwner": "o/a"}, author={"login": "me"}),
            node(2, updatedAt="2026-11-10T10:00:00Z", repository={"nameWithOwner": "o/b"}, author={"login": "me"}),
        ]
        return {"search": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}, None

    monkeypatch.setattr(github, "_graphql", fake_graphql)
    items, error = await github.fetch_mine()
    assert error is None and [i.pr.number for i in items] == [2, 1]
    assert "author:@me" in queries[0] and "statusCheckRollup" in queries[0]
    assert "statusCheckRollup" not in github._review_query("review-requested:@me")


def test_to_pr_reads_size():
    pr = github.to_pr(node(3, additions=342, deletions=2))
    assert (pr.additions, pr.deletions, pr.size) == (342, 2, 344)
    assert render.size_text(pr).plain == "+342 -2"
    assert "additions deletions" in github._review_query("author:@me")


async def test_fetch_org_saves_and_reads_back(monkeypatch, tmp_path):
    asked = []

    async def fake_search(qualifier, pages=4, fields=github._SEARCH_FIELDS):
        asked.append(qualifier)
        if "reviewed-by" in qualifier:
            return [{"url": "https://example.invalid/2"}], None
        if ">=" in qualifier:
            return [node(1, updatedAt="2026-11-01T10:00:00Z"), node(2, isDraft=True)], None
        return ([node(2, isDraft=True)] if "<" in qualifier else []), None  # same PR seen twice counts once

    monkeypatch.setattr(github, "_search_all", fake_search)
    monkeypatch.setattr(github, "_org_file", lambda: tmp_path / "org-prs.json")
    assert github.cached_org("GTNewHorizons") is None

    found = await github.fetch_org("GTNewHorizons")
    assert len(asked) == 7 and "org:GTNewHorizons reviewed-by:@me" in asked
    assert [i.pr.number for i in found.items] == [1, 2] and found.reviewed == {"https://example.invalid/2"}

    again = github.cached_org("GTNewHorizons")
    assert again and [i.pr.draft for i in again.items] == [False, True] and again.fetched == found.fetched
    assert github.cached_org("SomeOtherOrg") is None


def test_created_ranges_cover_everything_once():
    from datetime import date

    assert github._created_ranges(date(2026, 11, 18), (7, 30)) == [
        "created:>=2026-11-11",
        "created:2026-10-19..2026-11-10",
        "created:<2026-10-19",
    ]


def test_filter_prs():
    from datetime import UTC, datetime, timedelta

    from nh_workbench.models import PrInfo, ReviewItem

    now = datetime(2026, 11, 19, tzinfo=UTC)

    def item(repo: str, reviews: int | None, days: int) -> ReviewItem:
        pr = PrInfo(1, "OPEN", False, None, None, "", f"u/{repo}/{days}", "t", reviews=reviews)
        return ReviewItem(repo, pr, "a", now - timedelta(days=days), True)

    items = [item("O/Here", 0, 1), item("o/pack", 3, 40), item("o/else", None, 31)]
    local, in_pack = {"o/here"}, {"o/pack", "o/here"}

    def repos(*wanted: str) -> list[str]:
        return [i.repo for i in github.filter_prs(items, wanted, local, in_pack, now)]

    assert repos() == ["O/Here", "o/pack", "o/else"]
    assert repos("local") == ["O/Here"]
    assert repos("pack") == ["O/Here", "o/pack"]
    assert repos("unreviewed") == ["O/Here", "o/else"]  # no count yet: kept
    assert repos("stale") == ["o/pack", "o/else"]
    assert repos("stale", "pack") == ["o/pack"]


def test_to_pr_review_count():
    node = {"number": 1, "state": "OPEN"}
    assert github.to_pr(node).reviews is None
    assert github.to_pr({**node, "reviews": {"totalCount": 0}}).reviews == 0


def test_clone_urls_and_ssh_errors():
    from nh_workbench import git
    from nh_workbench.proc import Result

    assert git.clone_url("github.com", "o/m") == "git@github.com:o/m.git"
    assert git.clone_url("github.com", "o/m", "https") == "https://github.com/o/m.git"
    refused = Result(128, "", "git@github.com: Permission denied (publickey).\nfatal: Could not read")
    assert "HTTPS" in git.clone_error(refused)
    assert git.clone_error(Result(128, "", "fatal: repository not found")) == "fatal: repository not found"


def test_missing_pack_jobs(tmp_path):
    from nh_workbench.models import PackEntry, RepoState

    (tmp_path / "Taken").mkdir()
    entries = [
        PackEntry("A", "github", "1", repo="o/A"),
        PackEntry("A-extra", "github", "1", repo="o/A"),  # same repo, cloned once
        PackEntry("B", "external", "1", repo="x/B", host="gitlab.com", branch="1.7.10"),
        PackEntry("Private", "github", "1", repo="o/P", private=True),
        PackEntry("Here", "github", "1", repo="o/Here", local=RepoState(tmp_path / "Here", "Here")),
        PackEntry("Taken", "github", "1", repo="o/Taken"),
        PackEntry("Closed", "external", "1", closed=True),
    ]
    jobs = pack.missing_jobs(entries, tmp_path, "https")
    assert [(j.url, j.dest.name, j.branch, j.private) for j in jobs] == [
        ("https://github.com/o/A.git", "A", None, False),
        ("https://gitlab.com/x/B.git", "B", "1.7.10", False),
        ("https://github.com/o/P.git", "P", None, True),  # private: clones for accounts with access
    ]
    assert pack.missing_jobs(entries[:1], tmp_path)[0].url == "git@github.com:o/A.git"


def test_clone_choices_split_daily_and_release(tmp_path):
    from nh_workbench.config import Config
    from nh_workbench.models import PackEntry, PackInfo
    from nh_workbench.views import clone_choices, clone_list

    entries = [
        PackEntry("Both", "github", "1", repo="o/Both", release_version="0.9"),
        PackEntry("New", "github", "1", repo="o/New"),  # in the daily only
        PackEntry("Gone", "github", "", repo="o/Gone", release_version="2", private=True),  # in the release only
    ]
    cfg = Config(root=tmp_path)
    actions, picks = clone_choices(cfg, PackInfo(entries=entries, release="2.8.4"))
    assert [j.name for j in picks["daily"][0]] == ["o/Both", "o/New"]
    assert [j.name for j in picks["release"][0]] == ["o/Both", "o/Gone"]
    assert picks["release"][1] == "mods of release 2.8.4"
    assert [a[1] for a in actions] == ["daily", "release", "org"] and all(a[3] is None for a in actions)
    assert clone_list(picks["release"][0]) == "o/Both\no/Gone  (private: needs access)"
    assert clone_choices(cfg, None)[0][0][3] == "pack data not loaded"


async def test_org_repos_and_pr_head_use_the_protocol(monkeypatch):
    pages = iter(
        [
            {
                "organization": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                        "nodes": [{"nameWithOwner": "o/a"}, {"nameWithOwner": "o/empty", "isEmpty": True}],
                    }
                }
            },
            {
                "organization": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [{"nameWithOwner": "o/old", "isArchived": True}],
                    }
                }
            },
            {"repository": {"pullRequest": {"headRefName": "fix", "headRepository": {"nameWithOwner": "u/a"}}}},
        ]
    )

    async def fake_graphql(query, attempts=2):
        return next(pages), None

    monkeypatch.setattr(github, "_graphql", fake_graphql)
    repos, error = await github.org_repos("o")
    assert error is None and repos == [github.OrgRepo("o/a"), github.OrgRepo("o/old", archived=True)]
    head, _ = await github.pr_head("o/a", 3, "ssh")
    assert head.url == "git@github.com:u/a.git" and head.owner == "u"
