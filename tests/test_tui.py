import pytest
from textual.widgets import DataTable, Tabs

from gtnh_devenv_tools import github, pack
from gtnh_devenv_tools.models import PackEntry, PackInfo
from gtnh_devenv_tools.tui import RepoApp


@pytest.fixture
def offline(monkeypatch):
    async def no_prs(states):
        return github.PrData()

    loads = []

    async def fake_pack(cfg, force=False):
        loads.append(force)
        closed = PackEntry("Thaumcraft", "external", "4.2", closed=True)
        return PackInfo(entries=[closed, PackEntry("Missing", "github", "1", repo="o/m")], release="2.9.0")

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
