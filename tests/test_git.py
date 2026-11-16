from pathlib import Path

from gtnh_devenv_tools import git, ops
from gtnh_devenv_tools.config import Config, RepoConfig
from gtnh_devenv_tools.models import RepoState


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
        ]
    )
    branches, origin_head = git.parse_refs(text)
    by_name = {b.name: b for b in branches}
    assert origin_head == "master"
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
    assert [p.name for p in ops.discover(sandbox.cfg)] == ["ref", "repo"]
    cfg = Config(root=sandbox.root, repos={"ref": RepoConfig(ignore=True)})
    assert [p.name for p in ops.discover(cfg)] == ["repo"]


async def test_group_from_subfolder_and_config(sandbox):
    nested = sandbox.root / "_External" / "ref"
    nested.parent.mkdir()
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(nested))
    assert (await ops.scan_repo(sandbox.cfg, nested)).group == "_External"
    cfg = Config(root=sandbox.root, repos={"repo": RepoConfig(group="reference")})
    assert (await ops.scan_repo(cfg, sandbox.work)).group == "reference"
