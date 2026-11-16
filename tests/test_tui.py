import pytest
from textual.widgets import DataTable, Tabs

from gtnh_devenv_tools import github, pack
from gtnh_devenv_tools.models import PackEntry
from gtnh_devenv_tools.tui import RepoApp


@pytest.fixture
def offline(monkeypatch):
    async def no_prs(states):
        return github.PrData()

    async def fake_pack(cfg, force=False):
        closed = PackEntry("Thaumcraft", "external", "4.2", closed=True)
        return [closed, PackEntry("Missing", "github", "1", repo="o/m")]

    monkeypatch.setattr(github, "fetch_prs", no_prs)
    monkeypatch.setattr(pack, "load", fake_pack)


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


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
