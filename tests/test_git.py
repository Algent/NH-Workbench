from pathlib import Path

from nh_workbench import git, ops, render
from nh_workbench.config import Config, RepoConfig
from nh_workbench.models import PrHead, PrInfo, RepoState


def test_parse_status():
    text = "\n".join(
        [
            "# branch.oid 1234abcd",
            "# branch.head algent/thing",
            "# branch.upstream origin/algent/thing",
            "# branch.ab +2 -11",
            "# stash 3",
            "1 .M N... 100644 100644 100644 aaa bbb src/A.java",
            "2 R. N... 100644 100644 100644 aaa bbb R100 new.txt\told.txt",
            "u UU N... 100644 100644 100644 100644 aaa bbb ccc conflict.txt",
            "? notes.txt",
        ]
    )
    st = RepoState(Path("x"), "x")
    git.parse_status(text, st)
    assert (st.head, st.upstream, st.ahead, st.behind) == ("algent/thing", "origin/algent/thing", 2, 11)
    assert (st.changed, st.untracked, st.stash) == (3, 1, 3)


def test_parse_status_detached():
    st = RepoState(Path("x"), "x")
    git.parse_status("# branch.oid abc\n# branch.head (detached)\n", st)
    assert st.head is None and st.head_sha == "abc"


def test_parse_refs():
    def line(*parts: str) -> str:
        return "\x00".join(parts)

    text = "\n".join(
        [
            line("refs/heads/master", "a1", "origin/master", "behind 4", "2026-11-01T10:00:00+01:00", "", "subject"),
            line("refs/heads/algent/x", "b2", "origin/algent/x", "gone", "2026-11-02T10:00:00+01:00", "", "wip"),
            line("refs/heads/algent/y", "c3", "origin/algent/y", "ahead 92, behind 1", "", "", ""),
            line("refs/heads/local", "d4", "", "", "", "", "no upstream"),
            line("refs/remotes/origin/HEAD", "a1", "", "", "", "origin/master", "subject"),
            line("refs/remotes/origin/release/2.9.x", "e5", "", "", "", "", "backport"),
        ]
    )
    branches, origin_head, releases = git.parse_refs(text)
    by_name = {b.name: b for b in branches}
    assert origin_head == "master" and releases == ["release/2.9.x"]
    assert by_name["master"].behind == 4 and not by_name["master"].gone
    assert by_name["algent/x"].gone
    assert (by_name["algent/y"].ahead, by_name["algent/y"].behind) == (92, 1)
    assert by_name["local"].upstream is None


def test_parse_remote():
    assert git.parse_remote("origin", "https://github.com/GTNewHorizons/GT5-Unofficial.git").slug == (
        "GTNewHorizons/GT5-Unofficial"
    )
    assert git.parse_remote("origin", "git@github.com:embeddedt/ArchaicFix").slug == "embeddedt/ArchaicFix"
    assert git.parse_remote("origin", "https://example.com/x/y.git").slug is None


async def test_read_state_clean_clone(sandbox):
    st = await ops.scan_repo(sandbox.cfg, sandbox.work)
    assert st.error is None
    assert (st.head, st.tracked, st.off_main, st.dirty) == ("master", "master", False, False)
    assert st.subject == "first" and "origin" in st.remotes
    assert st.group == "other"


async def test_pull_fast_forwards_main(sandbox):
    sandbox.push_upstream("second")
    tip = sandbox.push_upstream("third")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert (st.note.kind, st.note.commits) == ("updated", 2)
    assert sandbox.sha(sandbox.work) == tip
    assert st.behind == 0


async def test_pull_up_to_date(sandbox):
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert st.note.kind == "current"


async def test_pull_skips_dirty_main(sandbox):
    before = sandbox.sha(sandbox.work)
    (sandbox.work / "first.txt").write_text("local edit", encoding="utf-8")
    sandbox.push_upstream("second")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert st.note.kind == "skipped" and "dirty" in st.note.text
    assert sandbox.sha(sandbox.work) == before
    assert (sandbox.work / "first.txt").read_text(encoding="utf-8") == "local edit"


async def test_pull_ignores_untracked_files(sandbox):
    (sandbox.work / "scratch.txt").write_text("x", encoding="utf-8")
    tip = sandbox.push_upstream("second")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert st.note.kind == "updated" and sandbox.sha(sandbox.work) == tip


async def test_pull_skips_local_commits_on_main(sandbox):
    local = sandbox.commit(sandbox.work, "oops-on-master")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert (st.note.kind, st.note.text) == ("skipped", "ahead 1")
    sandbox.push_upstream("second")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert (st.note.kind, st.note.text) == ("skipped", "diverged")
    assert sandbox.sha(sandbox.work) == local


async def test_pull_updates_main_from_feature_branch(sandbox):
    sandbox.git(sandbox.work, "checkout", "-b", "algent/feature")
    feature = sandbox.commit(sandbox.work, "feature-work")
    (sandbox.work / "first.txt").write_text("uncommitted", encoding="utf-8")
    tip = sandbox.push_upstream("second")

    st = await ops.pull_repo(sandbox.cfg, sandbox.work)

    assert (st.note.kind, st.note.text) == ("updated", "master +1")
    assert sandbox.sha(sandbox.work, "master") == tip
    assert st.head == "algent/feature" and sandbox.sha(sandbox.work) == feature
    assert (sandbox.work / "first.txt").read_text(encoding="utf-8") == "uncommitted"
    assert st.off_main and st.main_behind == 0


async def test_pull_refuses_diverged_main_from_feature_branch(sandbox):
    local = sandbox.commit(sandbox.work, "oops-on-master")
    sandbox.git(sandbox.work, "checkout", "-b", "algent/feature")
    sandbox.push_upstream("second")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert (st.note.kind, st.note.text) == ("skipped", "master diverged")
    assert sandbox.sha(sandbox.work, "master") == local


async def test_pull_from_detached_head(sandbox):
    sandbox.git(sandbox.work, "checkout", "--detach")
    tip = sandbox.push_upstream("second")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert st.head is None and st.note.kind == "updated"
    assert sandbox.sha(sandbox.work, "master") == tip


async def test_dry_run_touches_nothing(sandbox):
    tip = sandbox.push_upstream("second")
    before = sandbox.sha(sandbox.work)
    st = await ops.pull_repo(sandbox.cfg, sandbox.work, dry_run=True)
    assert st.note.kind == "current"  # not fetched, so nothing is known yet
    assert sandbox.sha(sandbox.work, "origin/master") == before

    sandbox.git(sandbox.work, "fetch")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work, dry_run=True)
    assert (st.note.kind, st.note.commits) == ("planned", 1)
    assert sandbox.sha(sandbox.work) == before and sandbox.sha(sandbox.work, "origin/master") == tip


async def test_fetch_reports_gone_upstream(sandbox):
    sandbox.git(sandbox.work, "checkout", "-b", "algent/merged")
    sandbox.commit(sandbox.work, "work")
    sandbox.git(sandbox.work, "push", "-u", "origin", "algent/merged")
    sandbox.git(sandbox.work, "checkout", "master")
    sandbox.git(sandbox.seed, "push", "origin", "--delete", "algent/merged")

    st = await ops.fetch_repo(sandbox.cfg, sandbox.work)
    assert st.note.kind == "info"
    assert st.branch("algent/merged").gone


async def test_fetch_failure_is_reported(sandbox):
    sandbox.git(sandbox.work, "remote", "set-url", "origin", str(sandbox.origin) + "-missing")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert st.note.kind == "failed" and "fetch failed" in st.note.text


async def test_dead_fork_remote_is_only_a_warning(sandbox):
    sandbox.git(sandbox.work, "remote", "add", "fork", str(sandbox.origin) + "-missing")
    tip = sandbox.push_upstream("second")
    st = await ops.pull_repo(sandbox.cfg, sandbox.work)
    assert st.note.kind == "updated" and "fork:" in st.note.text
    assert sandbox.sha(sandbox.work) == tip


async def test_tracked_branch_override(sandbox):
    sandbox.git(sandbox.work, "checkout", "-b", "1.7.10/develop")
    cfg = Config(root=sandbox.root, repos={"repo": RepoConfig(tracked_branch="1.7.10/develop")})
    st = await ops.scan_repo(cfg, sandbox.work)
    assert st.tracked == "1.7.10/develop" and not st.off_main
    assert (await ops.scan_repo(sandbox.cfg, sandbox.work)).off_main


async def test_to_main_after_merged_pr(sandbox):
    def pr(state: str) -> PrInfo:
        return PrInfo(1, state, False, None, None, "", "", "")

    sandbox.git(sandbox.work, "checkout", "-b", "algent/done")
    sandbox.commit(sandbox.work, "feature-work")
    (sandbox.work / "scratch.txt").write_text("untracked", encoding="utf-8")
    tip = sandbox.push_upstream("second")

    st = await ops.scan_repo(sandbox.cfg, sandbox.work)
    assert ops.to_main_candidates([st]) == []  # no PR known
    st.current.pr = pr("OPEN")
    assert ops.to_main_candidates([st]) == []
    st.current.pr = pr("MERGED")
    assert ops.to_main_candidates([st]) == [st]

    (sandbox.work / "first.txt").write_text("uncommitted", encoding="utf-8")
    dirty = await ops.scan_repo(sandbox.cfg, sandbox.work)
    dirty.current.pr = pr("MERGED")
    assert ops.to_main_candidates([dirty]) == []
    sandbox.git(sandbox.work, "checkout", "--", "first.txt")

    new = await ops.to_main(sandbox.cfg, st)
    assert (new.head, new.off_main, new.note.kind) == ("master", False, "updated")
    assert new.note.text == "on master, +1" and sandbox.sha(sandbox.work) == tip
    assert new.branch("algent/done") is not None
    assert (sandbox.work / "scratch.txt").exists()


async def test_release_branch_progress(sandbox):
    from nh_workbench import pack

    branch = pack.release_branch("1.2.0")
    assert branch == "release/1.2.x" and pack.release_branch("2.9.0") == "release/2.9.x"

    sandbox.git(sandbox.seed, "tag", "1.2.0")
    sandbox.git(sandbox.seed, "checkout", "-b", branch)
    sandbox.commit(sandbox.seed, "backport-1")
    sandbox.commit(sandbox.seed, "backport-2")
    sandbox.git(sandbox.seed, "push", "origin", branch, "--tags")

    st = await ops.fetch_repo(sandbox.cfg, sandbox.work)
    assert st.release_branches == [branch] and st.head == "master"

    await ops.pack_progress([st], branch)
    assert st.release_ahead is None and st.daily_ahead is None  # not known to be in the pack

    st.release = "1.2.0"
    await ops.pack_progress([st], branch)
    assert st.release_ahead == 2 and st.release_commits == ["backport-2", "backport-1"]

    st.release = "9.9.9"  # version whose tag does not exist here
    await ops.pack_progress([st], branch)
    assert st.release_ahead is None and st.release_commits == []

    # main moved on after the version the daily ships
    sandbox.git(sandbox.seed, "checkout", "master")
    sandbox.push_upstream("unreleased-1")
    sandbox.push_upstream("unreleased-2")
    st = await ops.fetch_repo(sandbox.cfg, sandbox.work)
    st.daily = "1.2.0"
    await ops.pack_progress([st])
    assert st.daily_ahead == 2 and st.behind == 2  # counted on origin/master, the checkout itself is behind
    assert st.release_delta is None

    st.release = "1.2.0"
    await ops.pack_progress([st])
    assert st.release_delta == 2  # main moved two commits past the released tag
    assert "Δ2" in render.release_text(st).plain


async def test_delete_branch_checks_tip(sandbox):
    sandbox.git(sandbox.work, "branch", "old")
    tip = sandbox.sha(sandbox.work, "old")
    assert not (await git.delete_branch(sandbox.work, "old", "0" * 40)).ok
    assert (await git.delete_branch(sandbox.work, "old", tip)).ok
    assert "old" not in sandbox.git(sandbox.work, "branch", "--list", "old")


def test_discover_groups(sandbox):
    nested = sandbox.root / "_External" / "ref"
    nested.parent.mkdir()
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(nested))
    (sandbox.root / "not-a-repo").mkdir()
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(sandbox.root / ".github"))
    hidden = sandbox.root / ".idea" / "inside"  # a dot folder that is no repo is not a group either
    hidden.parent.mkdir()
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(hidden))
    assert [p.name for p in ops.discover(sandbox.cfg)] == [".github", "ref", "repo"]
    cfg = Config(root=sandbox.root, repos={"ref": RepoConfig(ignore=True)})
    assert [p.name for p in ops.discover(cfg)] == [".github", "repo"]


async def test_group_from_subfolder_and_config(sandbox):
    nested = sandbox.root / "_External" / "ref"
    nested.parent.mkdir()
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(nested))
    assert (await ops.scan_repo(sandbox.cfg, nested)).group == "_External"
    cfg = Config(root=sandbox.root, repos={"repo": RepoConfig(group="reference")})
    assert (await ops.scan_repo(cfg, sandbox.work)).group == "reference"


def test_parse_worktrees(tmp_path):
    main, linked = tmp_path / "repo", tmp_path / "repo-fix"
    text = (
        f"worktree {main.as_posix()}\nHEAD a1\nbranch refs/heads/master\n\n"
        f"worktree {linked.as_posix()}\nHEAD b2\nbranch refs/heads/algent/fix\n\n"
        f"worktree {(tmp_path / 'loose').as_posix()}\nHEAD c3\ndetached\n"
    )
    others, parent = git.parse_worktrees(text, main)
    assert parent is None and [(t.path.name, t.branch) for t in others] == [("repo-fix", "algent/fix"), ("loose", None)]
    others, parent = git.parse_worktrees(text, linked)
    assert parent == main and [t.path.name for t in others] == ["repo", "loose"]
    assert git.parse_worktrees("", main) == ([], None)


async def test_worktrees_are_recognised_and_respected(sandbox):
    linked = sandbox.root / "repo-fix"
    sandbox.git(sandbox.work, "worktree", "add", "-b", "algent/fix", str(linked))
    assert [p.name for p in ops.discover(sandbox.cfg)] == ["repo", "repo-fix"]

    main = await ops.scan_repo(sandbox.cfg, sandbox.work)
    wt = await ops.scan_repo(sandbox.cfg, linked)
    assert main.worktree_of is None and [t.branch for t in main.worktrees] == ["algent/fix"]
    assert wt.worktree_of.resolve() == sandbox.work.resolve() and wt.repo_root == wt.worktree_of
    assert wt.head == "algent/fix" and wt.off_main and wt.checked_out_elsewhere("master")
    assert main.checked_out_elsewhere("algent/fix") and not main.checked_out_elsewhere("master")

    tip = sandbox.push_upstream("second")
    states = await ops.run_all([sandbox.work, linked], lambda p: ops.pull_repo(sandbox.cfg, p), 8)
    by_name = {st.name: st for st in states}
    assert by_name["repo"].note.kind == "updated" and sandbox.sha(sandbox.work) == tip
    # The linked tree leaves master to the tree that has it checked out, and says so.
    assert by_name["repo-fix"].note.kind in ("current", "updated")
    assert sandbox.sha(linked, "master") == tip

    merged = PrInfo(1, "MERGED", False, None, None, wt.head_sha, "", "")
    wt = await ops.scan_repo(sandbox.cfg, linked)
    wt.current.pr = merged
    assert ops.to_main_candidates([wt]) == []  # master is in use by the main working tree

    main = await ops.scan_repo(sandbox.cfg, sandbox.work)
    main.branch("algent/fix").pr = merged
    safe, kept = await ops.cleanup_candidates([main])
    assert safe == [] and kept == []  # the branch is checked out in the linked tree


async def test_linked_worktree_skips_main_held_elsewhere(sandbox):
    linked = sandbox.root / "repo-fix"
    sandbox.git(sandbox.work, "worktree", "add", "-b", "algent/fix", str(linked))
    sandbox.push_upstream("second")
    sandbox.git(sandbox.work, "fetch")
    wt = await ops.scan_repo(sandbox.cfg, linked)
    outcome = await git.safe_pull(wt)
    assert (outcome.kind, outcome.text) == ("current", "master lives in repo")


def test_parse_worktrees_ignores_stale_entries(tmp_path):
    main = tmp_path / "repo"
    text = (
        f"worktree {main.as_posix()}\nHEAD a1\nbranch refs/heads/master\n\n"
        f"worktree {(tmp_path / 'gone').as_posix()}\nHEAD d4\ndetached\nprunable gitdir file points nowhere\n"
    )
    assert git.parse_worktrees(text, main) == ([], None)


def make_fork(sandbox, branch: str = "feature") -> tuple[Path, str]:
    """A fork of origin on disk with one extra branch; returns its path and the branch tip."""
    fork = sandbox.root.parent / "fork.git"
    sandbox.git(sandbox.root.parent, "clone", "--bare", str(sandbox.origin), str(fork))
    sandbox.git(sandbox.seed, "checkout", "-b", branch)
    tip = sandbox.commit(sandbox.seed, "fork-work")
    sandbox.git(sandbox.seed, "push", str(fork), branch)
    sandbox.git(sandbox.seed, "checkout", "master")
    return fork, tip


async def test_checkout_fork_pr_as_pr_branch_tracking_the_fork(sandbox):
    fork, tip = make_fork(sandbox)
    head = PrHead(7, "org/repo", "feature", "someone/repo", str(fork))
    st = await ops.checkout_pr(sandbox.cfg, await ops.scan_repo(sandbox.cfg, sandbox.work), head)
    assert st.note.kind == "updated", st.note.text
    assert st.head == "PR-7" and st.head_sha == tip
    assert st.current.upstream == "someone/feature" and "someone" in st.remotes
    # The cleanup drops the fork remote along with the branch.
    assert [r.name for _, r in ops.orphaned_remotes([(st, st.current)])] == ["someone"]

    sandbox.git(sandbox.seed, "checkout", "feature")
    newer = sandbox.commit(sandbox.seed, "more-fork-work")
    sandbox.git(sandbox.seed, "push", str(fork), "feature")
    sandbox.git(sandbox.work, "switch", "master")
    st = await ops.checkout_pr(sandbox.cfg, await ops.scan_repo(sandbox.cfg, sandbox.work), head)
    assert st.note.kind == "updated" and st.head == "PR-7" and st.head_sha == newer
    assert [b.name for b in st.branches] == ["PR-7", "master"]


async def test_checkout_origin_pr_keeps_its_name(sandbox):
    sandbox.git(sandbox.seed, "checkout", "-b", "algent/thing")
    tip = sandbox.commit(sandbox.seed, "thing")
    sandbox.git(sandbox.seed, "push", "origin", "algent/thing")
    head = PrHead(3, "org/repo", "algent/thing", "org/repo", str(sandbox.origin))
    st = await ops.checkout_pr(sandbox.cfg, await ops.scan_repo(sandbox.cfg, sandbox.work), head)
    assert st.head == "algent/thing" and st.head_sha == tip and st.current.upstream == "origin/algent/thing"
    assert list(st.remotes) == ["origin"]


async def test_checkout_pr_of_deleted_fork_and_dirty_repo(sandbox):
    sandbox.git(sandbox.seed, "checkout", "-b", "gone")
    tip = sandbox.commit(sandbox.seed, "orphan")
    sandbox.git(sandbox.seed, "push", "origin", "gone:refs/pull/9/head")
    head = PrHead(9, "org/repo", "gone", None)

    (sandbox.work / "first.txt").write_text("changed", encoding="utf-8")
    st = await ops.checkout_pr(sandbox.cfg, await ops.scan_repo(sandbox.cfg, sandbox.work), head)
    assert st.note.kind == "failed" and st.head == "master"
    sandbox.git(sandbox.work, "checkout", "--", "first.txt")

    st = await ops.checkout_pr(sandbox.cfg, await ops.scan_repo(sandbox.cfg, sandbox.work), head)
    assert st.note.kind == "updated" and st.head == "PR-9" and st.head_sha == tip and st.current.upstream is None


async def test_cleanup_offers_the_checked_out_branch_only_when_parked(sandbox):
    sandbox.git(sandbox.work, "checkout", "-b", "algent/done")
    tip = sandbox.commit(sandbox.work, "feature-work")
    st = await ops.scan_repo(sandbox.cfg, sandbox.work)
    st.current.pr = PrInfo(1, "MERGED", False, None, None, tip, "", "")
    assert (await ops.cleanup_candidates([st]))[0] == []
    assert (await ops.cleanup_candidates([st], parked=True))[0] == [(st, st.current)]

    (sandbox.work / "first.txt").write_text("uncommitted", encoding="utf-8")
    dirty = await ops.scan_repo(sandbox.cfg, sandbox.work)
    dirty.current.pr = st.current.pr
    assert (await ops.cleanup_candidates([dirty], parked=True))[0] == []


async def test_org_jobs_and_clone_all(sandbox):
    from nh_workbench.github import OrgRepo
    from nh_workbench.models import CloneJob

    sandbox.git(sandbox.work, "remote", "add", "gh", "https://github.com/o/repo.git")
    st = await ops.scan_repo(sandbox.cfg, sandbox.work)
    (sandbox.root / "taken").mkdir()
    repos = [OrgRepo("o/repo"), OrgRepo("O/New"), OrgRepo("o/taken")]
    jobs = ops.org_jobs(repos, [st], sandbox.root, "https")
    assert [(j.name, j.url, j.dest) for j in jobs] == [("O/New", "https://github.com/O/New.git", sandbox.root / "New")]

    good = CloneJob("o/good", str(sandbox.origin), sandbox.root / "good")
    bad = CloneJob("o/bad", str(sandbox.origin) + "-missing", sandbox.root / "bad")
    seen = []
    failed = await ops.clone_all([good, bad], 2, lambda job, why: seen.append((job.name, why is None)))
    assert sorted(seen) == [("o/bad", False), ("o/good", True)]
    assert [job for job, _ in failed] == [bad] and (sandbox.root / "good" / ".git").exists()


async def test_built_jars_of_org_repos(sandbox):
    sandbox.git(sandbox.work, "remote", "add", "gh", "https://github.com/GTNewHorizons/repo.git")
    other = sandbox.root / "other"
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(other))
    for repo in (sandbox.work, other):
        libs = repo / "build" / "libs"
        libs.mkdir(parents=True)
        for name in ("mod-1.0.jar", "mod-1.0-dev.jar", "notes.txt"):
            (libs / name).write_text("x" * 10, encoding="utf-8")
    states = [await ops.scan_repo(sandbox.cfg, p) for p in (sandbox.work, other)]

    found = ops.built_jars(states, "gtnewhorizons")
    assert [(st.name, [p.name for p in jars]) for st, jars in found] == [("repo", ["mod-1.0-dev.jar", "mod-1.0.jar"])]
    freed, failed = ops.delete_files([p for _, jars in found for p in jars])
    assert freed == 20 and failed == []
    assert sorted(p.name for p in (sandbox.work / "build" / "libs").iterdir()) == ["notes.txt"]
    assert len(list((other / "build" / "libs").iterdir())) == 3  # not an org repo: untouched
