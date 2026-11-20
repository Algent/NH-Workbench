import time
from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import DataTable, Tabs

from gtnh_devenv_tools import github, pack
from gtnh_devenv_tools.models import PackEntry, PackInfo, PrInfo, ReviewItem
from gtnh_devenv_tools.tui import RepoApp

org_searches: list[str] = []


@pytest.fixture
def offline(monkeypatch):
    async def no_prs(states):
        return github.PrData()

    loads = []

    async def fake_pack(cfg, force=False):
        loads.append(force)
        closed = PackEntry("Thaumcraft", "external", "4.2", closed=True)
        entries = [closed, PackEntry("Missing", "github", "1", repo="o/m")]
        return PackInfo(entries=entries, release="2.9.0", daily_build=836)

    async def fake_reviews(team=False):
        def item(number: int, direct: bool) -> ReviewItem:
            url = f"https://example.invalid/pull/{number}"
            pr = PrInfo(number, "OPEN", False, None, None, "", url, f"PR {number}", 500 if direct else 10, 1)
            return ReviewItem("o/m", pr, "ann", None, direct)

        return [item(1, True)] + ([item(2, False)] if team else []), None

    async def fake_mine():
        url = "https://example.invalid/pull/7"
        pr = PrInfo(7, "OPEN", False, "REVIEW_REQUIRED", "FAILURE", "", url, "My change")
        return [ReviewItem("o/m", pr, "me", None, True)], None

    async def fake_org(org):
        org_searches.append(org)

        def item(number: int, draft: bool = False, repo: str = "o/m", reviews: int = 0, days: int = 1) -> ReviewItem:
            url = f"https://example.invalid/pull/{number}"
            pr = PrInfo(number, "OPEN", draft, None, None, "", url, f"PR {number}", reviews=reviews)
            return ReviewItem(repo, pr, "bob", datetime.now(UTC) - timedelta(days=days), True)

        items = [item(1), item(7), item(20, draft=True), item(21, repo="o/other", reviews=2, days=90)]
        return github.OrgPrs(items, {"https://example.invalid/pull/21"}, time.time())

    org_searches.clear()
    monkeypatch.setattr(github, "fetch_org", fake_org)
    monkeypatch.setattr(github, "cached_org", lambda org: None)
    monkeypatch.setattr(github, "fetch_mine", fake_mine)
    monkeypatch.setattr(github, "fetch_reviews", fake_reviews)
    monkeypatch.setattr(github, "fetch_prs", no_prs)
    monkeypatch.setattr(pack, "load", fake_pack)
    return loads


async def settle(app, pilot):
    await pilot.pause()  # let messages like a tab change land before waiting on the work they start
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_repo_menu_acts_on_one_repo(sandbox, offline):
    from gtnh_devenv_tools.tui import RepoMenu

    other = sandbox.root / "second"
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(other))
    sandbox.git(sandbox.work, "checkout", "-b", "algent/feature")
    before = sandbox.sha(other)
    tip = sandbox.push_upstream("second")

    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)

        await pilot.press("enter")  # cursor starts on "repo"
        await pilot.pause()
        assert isinstance(app.screen, RepoMenu)
        await pilot.press("escape")
        await settle(app, pilot)
        assert not isinstance(app.screen, RepoMenu) and sandbox.sha(sandbox.work, "master") == before

        await pilot.press("enter", "m")
        await settle(app, pilot)
        st = app.states[str(sandbox.work)]
        assert (st.head, st.note.text) == ("master", "on master, +1")
        assert sandbox.sha(sandbox.work) == tip and sandbox.sha(other) == before

        await pilot.press("enter", "m")  # already on master: the option is disabled
        await pilot.pause()
        assert isinstance(app.screen, RepoMenu)
        await pilot.press("q")  # closes the menu, does not quit the app
        await pilot.pause()
        assert app.is_running and not isinstance(app.screen, RepoMenu)

        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#close")
        await pilot.pause()
        assert not isinstance(app.screen, RepoMenu)

        await pilot.click("#repos", offset=(4, 2))  # second row: the first click only highlights
        await pilot.pause()
        assert not isinstance(app.screen, RepoMenu)
        await pilot.click("#repos", offset=(4, 2))
        await pilot.pause()
        assert isinstance(app.screen, RepoMenu) and app.screen._st.name == "second"
        await pilot.press("down", "enter")  # second option: pull
        await settle(app, pilot)
        assert sandbox.sha(other) == tip


async def test_reviews_tab_opens_pr_in_browser(sandbox, offline, monkeypatch):
    from gtnh_devenv_tools import tui

    opened = []
    monkeypatch.setattr(tui.webbrowser, "open", opened.append)

    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        tabs = app.query_one(Tabs)
        assert str(tabs.query_one("#view-reviews").label) == "Reviews 1"  # only the direct request is counted
        tabs.active = "view-reviews"
        await settle(app, pilot)
        table = app.query_one("#reviews", DataTable)
        assert table.row_count == 1 and table.display and not app.query_one("#repos").display  # team ones not loaded

        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("t")  # now also the requests that come through a team
        await settle(app, pilot)
        assert table.row_count == 2 and str(tabs.query_one("#view-reviews").label) == "Reviews 1"
        await pilot.press("down", "w")
        await pilot.pause()
        assert opened == ["https://example.invalid/pull/1", "https://example.invalid/pull/2"]

        await pilot.press("t")
        await settle(app, pilot)
        assert table.row_count == 1

        tabs.active = "view-all"
        await settle(app, pilot)
        assert app.query_one("#repos").display and not table.display


async def test_sorting_by_key_and_header(sandbox, offline):
    other = sandbox.root / "second"
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(other))
    sandbox.git(other, "checkout", "-b", "algent/feature")

    def first_row(app) -> str:
        table = app.query_one("#repos", DataTable)
        return str(table.get_row_at(0)[0])

    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        assert app.sort == "name" and first_row(app) == "repo"
        await pilot.press("s", "s", "s")
        await pilot.pause()
        assert app.sort == "branch" and first_row(app) == "second"  # the one off main comes first
        assert "sorted by off main first" in app.sub_title
        await pilot.press("s")
        await pilot.pause()
        assert app.sort == "name" and first_row(app) == "repo"
        await pilot.click("#repos", offset=(30, 0))  # the Branch header
        await pilot.pause()
        assert app.sort == "branch"


async def test_group_and_hide_are_saved_to_config(sandbox, offline, monkeypatch, tmp_path):
    from textual.widgets import Input

    from gtnh_devenv_tools import config
    from gtnh_devenv_tools.tui import ConfirmScreen, PromptScreen

    cfg_file = tmp_path / "user" / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(sandbox.root / "second"))

    app = RepoApp(sandbox.cfg, root_override=sandbox.root)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter", "u")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        app.screen.query_one(Input).value = "reference"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.states[str(sandbox.work)].group == "reference" and app.needle == ""
        assert 'group = "reference"' in cfg_file.read_text(encoding="utf-8")

        await pilot.press("enter", "h")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("enter")  # the focused button is No: Enter alone never confirms
        await settle(app, pilot)
        assert not isinstance(app.screen, ConfirmScreen) and not config.load(sandbox.root, cfg_file).repo("repo").ignore

        await pilot.press("enter", "h")
        await pilot.pause()
        await pilot.click("#yes")
        await settle(app, pilot)
        assert [st.name for st in app.states.values()] == ["second"]
        assert config.load(sandbox.root, cfg_file).repo("repo").ignore

        await pilot.press("x", "u")  # Extras, then "show a hidden repo again"
        await pilot.pause()
        await pilot.press("1")
        await settle(app, pilot)
        assert sorted(st.name for st in app.states.values()) == ["repo", "second"]
        assert app.cfg.hidden == [] and app.states[str(sandbox.work)].group == "reference"


async def test_extras_menu_forces_pack_refresh(sandbox, offline):
    from gtnh_devenv_tools.tui import ExtrasMenu

    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        assert offline == [False]
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ExtrasMenu)
        await pilot.press("r")
        await settle(app, pilot)
        assert offline == [False, True] and not isinstance(app.screen, ExtrasMenu)
        assert app.pack_info.release == "2.9.0"
        table = app.query_one("#pack", DataTable)
        assert [str(c.label) for c in table.columns.values()][2:4] == ["Daily (#836)", "Release (2.9.0)"]
        app.query_one(Tabs).active = "view-pack"
        await settle(app, pilot)
        assert table.row_count == 2


async def test_lists_repos_and_pulls(sandbox, offline):
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(sandbox.root / "second"))
    sandbox.git(sandbox.work, "checkout", "-b", "algent/feature")
    tip = sandbox.push_upstream("second")

    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        table = app.query_one("#repos", DataTable)
        assert table.row_count == 2

        await pilot.press("p")
        await settle(app, pilot)
        assert {st.note.kind for st in app.states.values()} == {"updated"}
        assert sandbox.sha(sandbox.work, "master") == tip

        await pilot.press("tab")
        await settle(app, pilot)
        assert app.view == "off" and table.row_count == 1

        app.query_one(Tabs).active = "view-pack"
        await settle(app, pilot)
        assert app.query_one("#pack", DataTable).row_count == 2

        await pilot.press("slash", "t", "h", "a")
        await settle(app, pilot)
        assert app.query_one("#pack", DataTable).row_count == 1
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.needle == "" and app.query_one("#pack", DataTable).row_count == 2


async def test_new_worktree_appears_on_rescan_under_its_repo(sandbox, offline):
    sandbox.git(sandbox.root, "clone", str(sandbox.origin), str(sandbox.root / "second"))
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        table = app.query_one("#repos", DataTable)
        assert table.row_count == 2

        sandbox.git(sandbox.work, "worktree", "add", "-b", "algent/zz", str(sandbox.root / "zz-tree"))
        await pilot.press("r")
        await settle(app, pilot)
        names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert names == ["repo", "↳ zz-tree (repo)", "second"]  # under its repo, not at the end


async def test_footer_follows_the_view(sandbox, offline):
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)

        def shown() -> set[str]:
            return {binding.binding.description for binding in app.screen.active_bindings.values() if binding.enabled}

        assert {"Fetch all", "Sort", "Web"} <= shown() and not {"Clone", "Team reviews"} & shown()
        app.query_one(Tabs).active = "view-pack"
        await settle(app, pilot)
        assert "Clone" in shown() and not {"Fetch all", "Sort", "Team reviews"} & shown()
        app.query_one(Tabs).active = "view-reviews"
        await settle(app, pilot)
        assert "Team reviews" in shown() and not {"Fetch all", "Clone"} & shown()


async def test_my_prs_tab(sandbox, offline, monkeypatch):
    from rich.console import Console

    from gtnh_devenv_tools import tui

    opened = []
    monkeypatch.setattr(tui.webbrowser, "open", opened.append)
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        tabs = app.query_one(Tabs)
        assert str(tabs.query_one("#view-mine").label) == "My PRs 1"
        tabs.active = "view-mine"
        await settle(app, pilot)
        table = app.query_one("#mine", DataTable)
        assert table.row_count == 1 and table.display
        row = [str(cell) for cell in table.get_row_at(0)]
        assert row[0] == "m" and "#7 open" in row[1] and "CI fail" in row[1] and row[4] == "—"
        console = Console(width=200)
        with console.capture() as captured:
            console.print(app.query_one("#detail").content)
        assert "branch not checked out here" in captured.get()
        await pilot.press("enter")
        await pilot.pause()
        assert opened == ["https://example.invalid/pull/7"]


async def test_main_branch_and_auto_refresh_settings(sandbox, offline, monkeypatch, tmp_path):
    from textual.widgets import Input

    from gtnh_devenv_tools import config
    from gtnh_devenv_tools.tui import PromptScreen

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    sandbox.git(sandbox.work, "branch", "1.7.10/develop")

    app = RepoApp(sandbox.cfg, root_override=sandbox.root)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter", "b")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        app.screen.query_one(Input).value = "1.7.10/develop"
        await pilot.press("enter")
        await settle(app, pilot)
        st = app.states[str(sandbox.work)]
        assert st.tracked == "1.7.10/develop" and st.off_main

        await pilot.press("enter", "b")  # empty goes back to the automatic choice
        await pilot.pause()
        app.screen.query_one(Input).value = ""
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.states[str(sandbox.work)].tracked == "master"

        await pilot.press("enter", "k")
        await pilot.pause()
        app.screen.query_one(Input).value = "1.7.10/develop, nope, 1.7.10/develop"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.states[str(sandbox.work)].branch("1.7.10/develop").keep
        assert app.cfg.repo("repo").keep_branches == ("1.7.10/develop", "nope")

        await pilot.press("x", "k")
        await pilot.pause()
        await pilot.press("2")  # sorted: ("repo", "1.7.10/develop"), ("repo", "nope")
        await settle(app, pilot)
        assert app.cfg.repo("repo").keep_branches == ("1.7.10/develop",)
        await pilot.press("x", "k")
        await pilot.pause()
        await pilot.press("1")
        await settle(app, pilot)
        assert app.cfg.kept == [] and "keep_branches" not in cfg_file.read_text(encoding="utf-8")
        assert not app.states[str(sandbox.work)].branch("1.7.10/develop").keep

        await pilot.press("x", "a")
        await pilot.pause()
        app.screen.query_one(Input).value = "soon"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.cfg.refresh_minutes == 15  # rejected, default kept
        await pilot.press("x", "a")
        await pilot.pause()
        app.screen.query_one(Input).value = "30"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.cfg.refresh_minutes == 30 and "refresh_minutes = 30" in cfg_file.read_text(encoding="utf-8")


async def test_auto_refresh_waits_for_interval_and_quiet_moment(sandbox, offline):
    from gtnh_devenv_tools.tui import ConfirmScreen

    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        assert offline == [False] and app.cfg.refresh_minutes == 15

        app.auto_refresh()  # interval not reached
        await settle(app, pilot)
        assert offline == [False]

        app.last_loaded -= 16 * 60
        app.push_screen(ConfirmScreen("Busy?", ""))
        await pilot.pause()
        app.auto_refresh()  # a dialog is open: wait
        await settle(app, pilot)
        assert offline == [False]
        await pilot.press("n")
        await pilot.pause()

        app.auto_refresh()
        await settle(app, pilot)
        assert offline == [False, False]

        app.cfg.refresh_minutes = 0
        app.last_loaded -= 999 * 60
        app.auto_refresh()  # turned off
        await settle(app, pilot)
        assert offline == [False, False]


async def test_pr_size_sort_and_filter(sandbox, offline):
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        app.query_one(Tabs).active = "view-reviews"
        await settle(app, pilot)
        await pilot.press("t")
        await settle(app, pilot)
        table = app.query_one("#reviews", DataTable)

        def numbers() -> list[str]:
            return [str(table.get_row_at(i)[2]).split()[0] for i in range(table.row_count)]

        assert numbers() == ["#1", "#2"] and str(table.get_row_at(0)[3]) == "+500 -1"
        await pilot.press("s")  # smallest first
        await settle(app, pilot)
        assert numbers() == ["#2", "#1"] and "smallest first" in app.sub_title
        await pilot.press("s")
        await settle(app, pilot)
        assert numbers() == ["#1", "#2"]

        await pilot.press("slash")
        await pilot.press(*"<100")
        await settle(app, pilot)
        assert numbers() == ["#2"]


async def test_all_prs_tab_loads_only_when_opened(sandbox, offline):
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        tabs = app.query_one(Tabs)
        assert org_searches == [] and str(tabs.query_one("#view-org").label) == "All PRs"

        tabs.active = "view-org"
        await settle(app, pilot)
        assert org_searches == [sandbox.cfg.org]
        table = app.query_one("#org", DataTable)
        rows = {str(table.get_row_at(i)[2]).split()[0]: str(table.get_row_at(i)[0]) for i in range(table.row_count)}
        assert rows == {"#1": "requested", "#7": "author", "#21": "reviewed"}  # the draft is hidden
        assert str(tabs.query_one("#view-org").label) == "All PRs 3" and "no drafts" in app.sub_title

        await pilot.press("t")
        await settle(app, pilot)
        assert table.row_count == 4

        scans = len(offline)
        await pilot.press("r")  # searches the org again instead of rescanning the repos
        await settle(app, pilot)
        assert org_searches == [sandbox.cfg.org] * 2 and len(offline) == scans

        tabs.active = "view-all"
        await settle(app, pilot)
        tabs.active = "view-org"  # still fresh: no new search
        await settle(app, pilot)
        assert len(org_searches) == 2


async def test_open_ide_asks_for_launcher_when_none_found(sandbox, offline, monkeypatch, tmp_path):
    from textual.widgets import Input

    from gtnh_devenv_tools import config, proc
    from gtnh_devenv_tools.tui import PromptScreen

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    monkeypatch.setattr(config, "find_ide", lambda cfg, scripts=None: cfg.ide_command)
    launched = []
    monkeypatch.setattr(proc, "launch", lambda *args: launched.append(args))
    ide = tmp_path / "idea.sh"
    ide.write_text("")

    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        app.screen.query_one(Input).value = str(tmp_path / "missing")
        await pilot.press("enter")
        await settle(app, pilot)
        assert launched == [] and not cfg_file.exists()  # a path that does not exist is refused

        await pilot.press("o")
        await pilot.pause()
        app.screen.query_one(Input).value = str(ide)
        await pilot.press("enter")
        await settle(app, pilot)
        assert launched == [(str(ide), str(sandbox.work))] and app.cfg.ide_command == str(ide)
        assert config.load(sandbox.root, cfg_file).ide_command == str(ide)

        await pilot.press("o")  # known now: no question
        await settle(app, pilot)
        assert len(launched) == 2 and not isinstance(app.screen, PromptScreen)


async def test_ide_can_be_turned_off(sandbox, offline, monkeypatch, tmp_path):
    from textual.widgets import Input

    from gtnh_devenv_tools import config, proc
    from gtnh_devenv_tools.tui import PromptScreen, RepoMenu

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    monkeypatch.setattr(config, "_toolbox_scripts", lambda: tmp_path / "none")
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    launched = []
    monkeypatch.setattr(proc, "launch", lambda *args: launched.append(args))

    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)

        def footer() -> set[str]:
            return {b.binding.description for b in app.screen.active_bindings.values() if b.enabled}

        assert "IDE" in footer()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        app.screen.query_one(Input).value = "off"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.cfg.ide_off and "IDE" not in footer() and launched == []
        loaded = config.load(sandbox.root, cfg_file)
        assert loaded.ide_off and loaded.ide_command is None and config.find_ide(loaded) is None

        await pilot.press("o")  # does nothing now, asks nothing
        await pilot.pause()
        assert not isinstance(app.screen, PromptScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RepoMenu)
        await pilot.press("o")  # greyed out in the menu
        await pilot.pause()
        assert isinstance(app.screen, RepoMenu)
        await pilot.press("q")
        await pilot.pause()

        await pilot.press("x", "i")  # back on: empty means find it automatically
        await pilot.pause()
        assert app.screen.query_one(Input).value == "off"
        app.screen.query_one(Input).value = ""
        await pilot.press("enter")
        await settle(app, pilot)
        assert not app.cfg.ide_off and "IDE" in footer()
        assert "ide_command" not in cfg_file.read_text(encoding="utf-8")


async def test_missing_root_is_asked_for(sandbox, offline, monkeypatch, tmp_path):
    from textual.widgets import Input

    from gtnh_devenv_tools import config
    from gtnh_devenv_tools.tui import PromptScreen

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    monkeypatch.setattr(config, "suggest_root", lambda home=None: None)
    cfg = config.Config(root=tmp_path / "nowhere")

    app = RepoApp(cfg, root_override=cfg.root)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen) and app.states == {}
        app.screen.query_one(Input).value = str(tmp_path / "still-nowhere")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)  # asked again
        app.screen.query_one(Input).value = str(sandbox.root)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.cfg.root == sandbox.root and str(sandbox.work) in app.states
        assert config.load(path=cfg_file).root == sandbox.root


async def test_check_out_pr_from_reviews(sandbox, offline, monkeypatch):
    from test_git import make_fork

    from gtnh_devenv_tools.models import PrHead

    fork, tip = make_fork(sandbox)
    sandbox.git(sandbox.work, "remote", "add", "gh", "https://github.com/o/m.git")  # matches the fake PR's repo

    async def fake_head(base, number, protocol="https"):
        return PrHead(number, base, "feature", "someone/m", str(fork)), None

    monkeypatch.setattr(github, "pr_head", fake_head)
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        app.query_one(Tabs).active = "view-reviews"
        await settle(app, pilot)
        await pilot.press("c")
        await settle(app, pilot)
        st = app.states[str(sandbox.work)]
        assert st.head == "PR-1" and st.head_sha == tip and st.current.upstream == "someone/feature"
        assert st.current.pr and st.current.pr.number == 1


async def test_cleanup_switches_parked_repos_to_main_first(sandbox, offline, monkeypatch, tmp_path):
    from gtnh_devenv_tools import config
    from gtnh_devenv_tools.tui import ConfirmScreen

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    for name in ("algent/one", "algent/two"):
        sandbox.git(sandbox.work, "checkout", "-q", "-b", name, "master")
        sandbox.commit(sandbox.work, name.replace("/", "-"))
    tips = {name: sandbox.sha(sandbox.work, name) for name in ("algent/one", "algent/two")}

    async def merged(states):
        data = github.PrData()
        for number, (name, tip) in enumerate(tips.items(), 1):
            data.prs[("repo", name)] = PrInfo(number, "MERGED", False, None, None, tip, "", "")
        return data

    monkeypatch.setattr(github, "fetch_prs", merged)
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)

        async def confirm_dialog() -> None:
            await pilot.press("d")
            for _ in range(100):  # the dialog's worker waits on the dialog, so settle() would hang
                await pilot.pause(0.05)
                if isinstance(app.screen, ConfirmScreen):
                    return
            raise AssertionError("no cleanup dialog")

        await confirm_dialog()  # unticked: the checked-out branch stays, and that choice is remembered
        await pilot.press("s", "y")
        await settle(app, pilot)
        st = app.states[str(sandbox.work)]
        assert st.head == "algent/two" and [b.name for b in st.branches] == ["algent/two", "master"]
        assert config.load(sandbox.root, cfg_file).cleanup_to_main is False

        await confirm_dialog()  # ticked again: switch to main, then delete
        await pilot.press("s", "y")
        await settle(app, pilot)
        st = app.states[str(sandbox.work)]
        assert st.head == "master" and [b.name for b in st.branches] == ["master"]
        assert config.load(sandbox.root, cfg_file).cleanup_to_main is True


async def test_all_prs_filters_use_the_loaded_list(sandbox, offline, monkeypatch, tmp_path):
    from gtnh_devenv_tools import config
    from gtnh_devenv_tools.tui import MenuScreen

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    config.save_setting(("root",), str(sandbox.root), cfg_file)  # settings reload the config from disk
    sandbox.git(sandbox.work, "remote", "add", "gh", "https://github.com/o/m.git")
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        app.query_one(Tabs).active = "view-org"
        await settle(app, pilot)
        table = app.query_one("#org", DataTable)
        scans = len(offline)

        def shown() -> set[str]:
            return {str(table.get_row_at(i)[2]).split()[0] for i in range(table.row_count)}

        async def toggle(*keys: str) -> None:
            await pilot.press("v")
            await pilot.pause()
            for key in keys:
                assert isinstance(app.screen, MenuScreen)
                await pilot.press(key)
                await pilot.pause()
            await pilot.press("q")
            await settle(app, pilot)

        assert shown() == {"#1", "#7", "#21"}
        await toggle("u")
        assert shown() == {"#1", "#7"} and "only no reviews yet" in app.sub_title
        assert config.load(sandbox.root, cfg_file).pr_filters == ("unreviewed",)
        await toggle("u", "s")
        assert shown() == {"#21"}
        await toggle("n", "l")
        assert shown() == {"#1", "#7"}
        await toggle("l", "p")
        assert shown() == {"#1", "#7"}  # o/m is a pack mod, o/other is not
        await toggle("n")
        assert shown() == {"#1", "#7", "#21"} and "pr_filters" not in cfg_file.read_text(encoding="utf-8")
        assert org_searches == [sandbox.cfg.org] and len(offline) == scans  # nothing searched or rescanned again


async def test_auto_refresh_can_fetch(sandbox, offline, monkeypatch, tmp_path):
    from gtnh_devenv_tools import config, ops

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    config.save_setting(("root",), str(sandbox.root), cfg_file)  # settings reload the config from disk
    fetched = []
    real_fetch = ops.fetch_repo

    async def counting_fetch(cfg, path):
        fetched.append(path)
        return await real_fetch(cfg, path)

    monkeypatch.setattr(ops, "fetch_repo", counting_fetch)
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        app.last_loaded = float("-inf")  # monotonic time starts at boot: CI machines are young
        app.auto_refresh()
        await settle(app, pilot)
        assert fetched == []  # off by default: only a rescan

        scans = len(offline)
        await pilot.press("x", "f")
        await settle(app, pilot)
        assert app.cfg.refresh_fetch and len(offline) == scans  # saving the option does not rescan
        assert config.load(sandbox.root, cfg_file).refresh_fetch
        app.last_loaded = float("-inf")  # monotonic time starts at boot: CI machines are young
        app.auto_refresh()
        await settle(app, pilot)
        assert fetched == [sandbox.work]


async def test_extras_clone_protocol_and_clone_missing(sandbox, offline, monkeypatch, tmp_path):
    from gtnh_devenv_tools import config, git
    from gtnh_devenv_tools.tui import ConfirmScreen, MenuScreen

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    config.save_setting(("root",), str(sandbox.root), cfg_file)
    urls = []

    async def fake_clone(url, dest, branch=None):
        urls.append(url)
        return await real_clone(str(sandbox.origin), dest)

    real_clone = git.clone
    monkeypatch.setattr(git, "clone", fake_clone)

    async def fake_org_repos(org):
        return [
            github.OrgRepo("o/new"),
            github.OrgRepo("o/old", archived=True),
            github.OrgRepo("o/fork", fork=True),
        ], None

    monkeypatch.setattr(github, "org_repos", fake_org_repos)
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        assert app.cfg.clone_protocol == "ssh"
        await pilot.press("x", "s")
        await settle(app, pilot)
        assert config.load(sandbox.root, cfg_file).clone_protocol == "https"

        await pilot.press("x", "m")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)
        await pilot.press("p")  # the fake pack has one missing mod, o/m
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(app, pilot)
        assert urls == ["https://github.com/o/m.git"] and str(sandbox.root / "m") in app.states

        await pilot.press("x", "m", "o")  # not archived, forks included (most GTNH mods are forks)
        for _ in range(50):  # the list comes from a (fake) GitHub call first
            await pilot.pause()
            if isinstance(app.screen, ConfirmScreen):
                break
        assert isinstance(app.screen, ConfirmScreen) and "Clone 2 " in app.screen._title
        await pilot.press("y")
        await settle(app, pilot)
        assert sorted(urls[1:]) == ["https://github.com/o/fork.git", "https://github.com/o/new.git"]
        assert (sandbox.root / "new" / ".git").exists() and str(sandbox.root / "fork") in app.states


async def test_extras_deletes_built_jars(sandbox, offline):
    from gtnh_devenv_tools.tui import ConfirmScreen

    sandbox.git(sandbox.work, "remote", "add", "gh", "https://github.com/GTNewHorizons/repo.git")
    libs = sandbox.work / "build" / "libs"
    libs.mkdir(parents=True)
    (libs / "mod.jar").write_text("jar", encoding="utf-8")
    app = RepoApp(sandbox.cfg)
    async with app.run_test(size=(160, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("x", "b")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "Delete 1 built jars" in app.screen._title
        await pilot.press("n")  # keeps them
        await pilot.pause()
        assert (libs / "mod.jar").exists()
        await pilot.press("x", "b")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert not (libs / "mod.jar").exists() and libs.is_dir()


async def test_tools_tab_spark(sandbox, offline, monkeypatch, tmp_path):
    from test_spark import profile_bytes
    from textual.widgets import Input, TextArea

    from gtnh_devenv_tools import spark, tui_spark
    from gtnh_devenv_tools.tui import MenuScreen
    from gtnh_devenv_tools.tui_spark import SparkScreen

    cache = tmp_path / "cache"
    (cache / "spark").mkdir(parents=True)
    (cache / "spark" / "Saved12345.sparkprofile").write_bytes(profile_bytes())
    monkeypatch.setattr(spark, "cache_dir", lambda: cache)
    monkeypatch.setattr(tui_spark, "cache_dir", lambda: cache)
    copied = []

    app = RepoApp(sandbox.cfg)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(app, pilot)
        app.query_one(Tabs).active = "view-tools"
        await settle(app, pilot)
        assert app.query_one("#tools", DataTable).row_count == 1 and "spark link" in str(
            app.query_one("#detail").content
        )
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SparkScreen)
        assert screen.query_one("#spark-recent").option_count == 2  # the saved profile and install

        def enabled(key: str) -> bool:
            found = screen.active_bindings.get(key)
            return bool(found and found.enabled)

        assert enabled("f2") and not enabled("t")

        async def until_loaded() -> None:
            for _ in range(100):
                await pilot.pause()
                if screen.text:
                    return

        screen.query_one(Input).value = "Saved12345"
        await pilot.press("enter")
        await until_loaded()
        report = screen.query_one(TextArea)
        assert screen.text.startswith("# spark profile Saved12345") and report.has_focus

        def cursor_to(needle: str) -> None:
            row = next(i for i, line in enumerate(screen.text.splitlines()) if needle in line)
            report.move_cursor((row, 0))

        cursor_to("a.Mod.helper")
        helper_row = report.cursor_location[0]
        await pilot.press("enter")  # what helper calls
        await pilot.pause()
        assert screen.method == "a.Mod.helper" and not screen.up and "What is called by" in screen.text
        await pilot.press("c")  # who calls the method on the cursor line (the first line: none), so it asks
        await pilot.pause()
        app.screen.query_one(Input).value = "helper"
        await pilot.press("enter")
        await settle(app, pilot)
        assert screen.up and "Who calls 'helper'" in screen.text

        await pilot.press("escape")  # back to what helper calls
        await pilot.pause()
        assert screen.method == "a.Mod.helper" and not screen.up
        await pilot.press("escape")  # back to the summary, on the line we left from
        await pilot.pause()
        assert screen.method is None and report.cursor_location[0] == helper_row and screen.history == []

        await pilot.press("enter")  # helper's calls, then its callers
        await pilot.pause()
        cursor_to("] Mod.helper")  # the tree line, after its [mod] tag
        await pilot.press("c")
        await settle(app, pilot)
        assert screen.up and len(screen.history) == 2
        await pilot.press("s")  # straight to the summary, forgetting the way there
        await pilot.pause()
        assert screen.method is None and screen.history == []
        cursor_to("101  ")  # a row of the worst minutes
        await pilot.press("enter")
        await pilot.pause()
        assert screen.windows == [101] and "Only window(s) 101" in screen.text
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)
        await pilot.press("a")  # whole recording again
        await settle(app, pilot)
        assert screen.windows is None
        await pilot.press("escape")  # undoes the minute choice
        await pilot.pause()
        assert screen.windows == [101]

        await pilot.press("y", "e")
        await pilot.pause()
        assert copied == [screen.text] and (cache / "spark" / "Saved12345-summary.md").is_file()
        from gtnh_devenv_tools import skill

        monkeypatch.setattr(skill, "_home", lambda: tmp_path)
        assert not enabled("f2") and enabled("t")  # F2 only where profiles are picked
        await pilot.press("o", "f2")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)
        await pilot.press("2")  # Codex
        await settle(app, pilot)
        assert skill.installed("codex") and not (tmp_path / ".claude").exists()
        await pilot.press("escape")  # back to the profile from "open other"
        await pilot.pause()
        assert not screen._picking()

        await pilot.press("q")  # leaves at once, whatever the history
        await pilot.pause()
        assert not isinstance(app.screen, SparkScreen) and app.is_running


def test_method_and_window_on_report_lines():
    from gtnh_devenv_tools.tui_spark import method_on_line, window_on_line

    assert method_on_line(" 41.8%  galaxyspace.core.Craters.makeCrater") == "galaxyspace.core.Craters.makeCrater"
    line = " 41.7%     3.4s            Craters.createCraters > Craters.makeCrater  (own 41.7%)"
    assert method_on_line(line) == "Craters.makeCrater"
    assert method_on_line(" 18.2%    1m04s    World.tick > ... 8 frames ... > MinecraftServer$2.run") == (
        "MinecraftServer$2.run"
    )
    assert method_on_line(" 58.3%  galaxyspace") == "galaxyspace"
    assert method_on_line(" 41.7%     3.4s  < [ServerUtilities] FileUtils.writeAtomic") == "FileUtils.writeAtomic"
    tagged = " 65.4%     5.3s    Server.load > [GalaxySpace] Craters.provideChunk  (own 5.1%)"
    assert method_on_line(tagged) == "Craters.provideChunk"
    assert method_on_line(" 17.3%  native code") is None and method_on_line("## Thread Server thread") is None
    assert window_on_line("29920107 2h19m      769  14.7      16.3 23518") == 29920107
    assert window_on_line(" 41.8%  a.B.c") is None
