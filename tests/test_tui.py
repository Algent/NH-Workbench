import pytest
from textual.widgets import DataTable, Tabs

from gtnh_devenv_tools import github, pack
from gtnh_devenv_tools.models import PackEntry, PackInfo, PrInfo, ReviewItem
from gtnh_devenv_tools.tui import RepoApp


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

    async def fake_reviews():
        def item(number: int, direct: bool) -> ReviewItem:
            url = f"https://example.invalid/pull/{number}"
            pr = PrInfo(number, "OPEN", False, None, None, "", url, f"PR {number}")
            return ReviewItem("o/m", pr, "ann", None, direct)

        return [item(1, True), item(2, False)], None

    monkeypatch.setattr(github, "fetch_reviews", fake_reviews)
    monkeypatch.setattr(github, "fetch_prs", no_prs)
    monkeypatch.setattr(pack, "load", fake_pack)
    return loads


async def settle(app, pilot):
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
        assert table.row_count == 2 and table.display and not app.query_one("#repos").display

        await pilot.press("enter")
        await pilot.press("down", "g")
        await pilot.pause()
        assert opened == ["https://example.invalid/pull/1", "https://example.invalid/pull/2"]

        tabs.active = "view-all"
        await settle(app, pilot)
        assert app.query_one("#repos").display and not table.display


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
