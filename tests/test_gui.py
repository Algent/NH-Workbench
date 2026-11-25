import os
import time
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("PySide6", reason="the desktop UI is an optional extra: uv sync --extra desktop")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from rich.text import Text  # noqa: E402

from nh_workbench import github, gui, pack  # noqa: E402
from nh_workbench.models import PackEntry, PackInfo, PrHead, PrInfo, ReviewItem  # noqa: E402
from nh_workbench.proc import Result  # noqa: E402

MY_PR = "https://example.invalid/pull/7"
searches: list[str] = []


@pytest.fixture
def window(sandbox, monkeypatch):
    async def no_prs(states):
        return github.PrData()

    async def fake_pack(cfg, force=False):
        closed = PackEntry("Thaumcraft", "external", "4.2", closed=True)
        return PackInfo(entries=[closed, PackEntry("Missing", "github", "1", repo="o/m")], release="2.9.0")

    async def fake_mine():
        pr = PrInfo(7, "OPEN", False, "REVIEW_REQUIRED", "FAILURE", "", MY_PR, "My change", 120, 30)
        return [ReviewItem("o/m", pr, "me", None, True)], None

    monkeypatch.setattr(github, "fetch_prs", no_prs)

    async def fake_reviews(team=False):
        def item(number: int, direct: bool) -> ReviewItem:
            url = f"https://example.invalid/pull/{number}"
            pr = PrInfo(number, "OPEN", False, None, None, "", url, f"PR {number}", 500 if direct else 10, 1)
            return ReviewItem("o/m", pr, "ann", None, direct)

        return [item(1, True)] + ([item(2, False)] if team else []), None

    async def fake_org(org):
        searches.append(org)

        def item(number: int, draft: bool = False, repo: str = "o/m", reviews: int = 0, days: int = 1) -> ReviewItem:
            url = f"https://example.invalid/pull/{number}"
            pr = PrInfo(number, "OPEN", draft, None, None, "", url, f"PR {number}", reviews=reviews)
            return ReviewItem(repo, pr, "bob", datetime.now(UTC) - timedelta(days=days), True)

        items = [item(1), item(7), item(20, draft=True), item(21, repo="o/other", reviews=2, days=90)]
        return github.OrgPrs(items, {"https://example.invalid/pull/21"}, time.time())

    searches.clear()
    monkeypatch.setattr(github, "fetch_org", fake_org)
    monkeypatch.setattr(github, "cached_org", lambda org: None)
    monkeypatch.setattr(github, "fetch_mine", fake_mine)
    monkeypatch.setattr(github, "fetch_reviews", fake_reviews)
    monkeypatch.setattr(pack, "load", fake_pack)
    QApplication.instance() or QApplication([])
    win = gui.MainWindow(sandbox.cfg)
    yield win
    win.close()


def wait_for(check, timeout: float = 20) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def loaded(window) -> None:
    wait_for(lambda: window.model.rows and not window.running and window.pack.source.rows)


def test_text_html_keeps_colors_and_escapes():
    app = QApplication.instance() or QApplication([])
    theme = gui.theme_for(app.palette())
    out = gui.text_html(Text.assemble(("a<b", "bold green"), " ", ("x", "dim")), theme)
    assert "a&lt;b" in out and "font-weight:600" in out and "color:#" in out
    whole = gui.text_html(Text("missing", "bold yellow"), theme)  # styled as a whole, without spans
    assert "font-weight:600" in whole and "color:#" in whole


def test_window_lists_repos_and_fetches(window, sandbox):
    loaded(window)
    assert [st.name for st in window.model.rows] == ["repo"]
    assert window.tabs.tabText(0).split() == ["All", "1"]
    assert "repo" in window.detail.toPlainText()

    window.filter.setText("nothing")
    assert window.repos.proxy.rowCount() == 0
    window.filter.setText("")
    assert window.repos.proxy.rowCount() == 1

    tip = sandbox.push_upstream("second")
    window.table.selectRow(0)
    window._repo_action("pull", window._selected())
    wait_for(lambda: window.model.rows[0].note is not None and not window.running)
    assert sandbox.sha(sandbox.work) == tip


def test_pack_tab_lists_mods_and_clones(window, sandbox, monkeypatch):
    cloned = []

    async def fake_clone(entry, root, protocol="ssh"):
        cloned.append(entry.repo)
        sandbox.git(root, "clone", str(sandbox.origin), str(root / "m"))
        return Result(0, "", "")

    monkeypatch.setattr(pack, "clone", fake_clone)
    loaded(window)
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("pack"))
    assert window.tabs.tabText(window.tabs.currentIndex()).split() == ["Pack", "2"]
    assert window.table is window.pack and window.stack.currentWidget() is window.pack
    assert window.pack.source.headers[3] == "Release (2.9.0)"

    window.filter.setText("miss")
    assert window.pack.proxy.rowCount() == 1
    window.pack.selectRow(0)
    assert window._selected_entry().name == "Missing" and window._selected() is None
    assert "press c to clone o/m" in window.detail.toPlainText()

    window.pack.setFocus()
    QTest.keyClick(window.pack, Qt.Key.Key_C)  # as in the terminal UI: c clones the selected mod
    wait_for(lambda: cloned and not window.running and len(window.model.rows) == 2)
    assert cloned == ["o/m"]
    assert sorted(st.name for st in window.model.rows) == ["m", "repo"]

    window.filter.setText("thaum")
    window.pack.selectRow(0)
    QTest.keyClick(window.pack, Qt.Key.Key_C)
    assert cloned == ["o/m"] and "closed source" in window.message.text()


def test_toolbar_follows_the_tab(window):
    loaded(window)

    def shown() -> set[str]:
        return {key for key, action in window.tab_actions.items() if action.isVisible()}

    assert shown() == {"fetch", "pull", "rescan", "open_web", "open_ide"}
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("pack"))
    assert shown() == {"rescan", "clone", "open_web", "open_ide"}  # what the TUI's footer shows on Pack
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("mine"))
    assert shown() == {"reload_prs", "open_web", "checkout"}  # no IntelliJ: o/m is not checked out here
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("reviews"))
    assert shown() == {"reload_prs", "open_web", "checkout", "toggle_team"}
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("org"))
    assert shown() == {"reload_org", "open_web", "checkout", "toggle_drafts", "pr_filters"}
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("tools"))
    assert shown() == {"rescan"} and not window.filter_action.isVisible()  # as the TUI's footer on Tools
    window.tabs.setCurrentIndex(0)
    assert "clone" not in shown() and "toggle_team" not in shown()


def test_my_prs_tab_lists_and_opens(window, monkeypatch):
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", opened.append)
    loaded(window)
    wait_for(lambda: window.mine.source.rows)
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("mine"))
    assert window.tabs.tabText(window.tabs.currentIndex()).split() == ["My", "PRs", "1"]
    assert window.table is window.mine and window._selected_pr().pr.number == 7
    cells = [window.mine.proxy.index(0, c).data() for c in range(len(window.mine.source.columns))]
    assert cells[0] == "m" and "#7 open" in cells[1] and cells[4] == "\u2014"
    assert "branch not checked out here" in window.detail.toPlainText()

    window.filter.setText("<100")  # the TUI's size filter: 150 changed lines is too many
    assert window.mine.proxy.rowCount() == 0
    window.filter.setText("my change")
    assert window.mine.proxy.rowCount() == 1

    window.mine.selectRow(0)
    window.mine.activated.emit(window.mine.currentIndex())  # Enter or double click opens the PR, as in the TUI
    assert opened == [MY_PR]
    window.mine.setFocus()
    QTest.keyClick(window.mine, Qt.Key.Key_C)
    assert "o/m is not checked out here" in window.message.text()


def test_my_prs_check_out(window, sandbox, monkeypatch):
    from test_git import make_fork

    fork, tip = make_fork(sandbox)
    sandbox.git(sandbox.work, "remote", "add", "gh", "https://github.com/o/m.git")  # matches the PR's repo

    async def fake_head(base, number, protocol="https"):
        return PrHead(number, base, "feature", "someone/m", str(fork)), None

    monkeypatch.setattr(github, "pr_head", fake_head)
    loaded(window)
    window._rescan()  # the remote was added after the first scan
    wait_for(lambda: not window.running and "gh" in window.model.rows[0].remotes and window.mine.source.rows)
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("mine"))
    window.mine.selectRow(0)
    assert window.tab_actions["open_ide"].isVisible()  # its repo is checked out here now
    window.tab_actions["checkout"].trigger()
    wait_for(lambda: window.model.rows[0].current and window.model.rows[0].current.pr)  # linked once checked out
    st = window.model.rows[0]
    assert st.head_sha == tip and st.current.pr and st.current.pr.number == 7
    assert window.mine.source.rows and "PR-7" in window.mine.proxy.index(0, 4).data()  # its local branch now


def test_reviews_tab_and_team_requests(window, monkeypatch):
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", opened.append)
    loaded(window)
    wait_for(lambda: window.reviews.source.rows)
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("reviews"))
    assert window.tabs.tabText(window.tabs.currentIndex()).split() == ["Reviews", "1"]
    assert window.table is window.reviews and window.reviews.proxy.rowCount() == 1  # team ones not loaded
    cells = [window.reviews.proxy.index(0, c).data() for c in range(len(window.reviews.source.columns))]
    assert cells[:3] == ["you", "m", cells[2]] and cells[4] == "ann" and "#1 open" in cells[2]
    assert "not checked out" in window.detail.toPlainText()

    window.reviews.setFocus()
    QTest.keyClick(window.reviews, Qt.Key.Key_T)  # as in the terminal UI: t adds the team requests
    wait_for(lambda: window.reviews.proxy.rowCount() == 2 and not window.running)
    assert window.tab_actions["toggle_team"].isChecked()
    assert window.tabs.tabText(window.tabs.currentIndex()).split() == ["Reviews", "1"]  # still only yours

    window.reviews.selectRow(1)
    window.reviews.activated.emit(window.reviews.currentIndex())  # Enter opens the PR
    assert opened == ["https://example.invalid/pull/2"]

    window.tab_actions["toggle_team"].trigger()
    wait_for(lambda: window.reviews.proxy.rowCount() == 1 and not window.running)
    assert not window.tab_actions["toggle_team"].isChecked()


def test_refresh_from_github_on_pr_tabs(window, monkeypatch):
    calls = []
    real = github.fetch_mine

    async def counted():
        calls.append(1)
        return await real()

    loaded(window)
    wait_for(lambda: window.mine.source.rows and not window.running)
    monkeypatch.setattr(github, "fetch_mine", counted)
    window.tabs.setCurrentIndex(gui.VIEW_KEYS.index("mine"))
    window.mine.setFocus()
    QTest.keyClick(window.mine, Qt.Key.Key_R)  # as in the terminal UI: r refreshes the PRs, not the repos
    wait_for(lambda: calls and not window.running)
    assert window.mine.source.rows and window.message.text() != "Scanning..."


def test_all_prs_tab(window, monkeypatch, tmp_path):
    from nh_workbench import config

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    loaded(window)
    wait_for(lambda: window.mine.source.rows and window.reviews.source.rows)
    org_tab = gui.VIEW_KEYS.index("org")
    assert window.tabs.tabText(org_tab) == "All PRs"  # not searched yet
    window.tabs.setCurrentIndex(org_tab)
    wait_for(lambda: window.org.source.rows)
    assert len(searches) == 1  # searched once the tab opened
    assert window.tabs.tabText(org_tab).split() == ["All", "PRs", "3"]  # the draft is left out
    marks = {window.org.proxy.index(r, 2).data().split()[0]: window.org.proxy.index(r, 0).data() for r in range(3)}
    assert marks == {"#1": "requested", "#7": "author", "#21": "reviewed"}

    window.org.setFocus()
    QTest.keyClick(window.org, Qt.Key.Key_T)  # t: drafts too
    assert window.tabs.tabText(org_tab).split() == ["All", "PRs", "4"]
    QTest.keyClick(window.org, Qt.Key.Key_T)

    QTimer.singleShot(200, lambda: QTest.keyClick(QApplication.activePopupWidget(), Qt.Key.Key_U))
    QTest.keyClick(window.org, Qt.Key.Key_V)  # v: show only, u: PRs without reviews
    assert window.cfg.pr_filters == ("unreviewed",) and window.org.proxy.rowCount() == 2
    assert config.load(path=tmp_path / "config.toml").pr_filters == ("unreviewed",)

    window.org.selectRow(0)
    assert "Enter or w: open" in window.detail.toPlainText()
    count = len(searches)
    QTest.keyClick(window.org, Qt.Key.Key_R)  # r: search again
    wait_for(lambda: len(searches) == count + 1 and not window.wb.org_searching)


def test_tools_tab(window, monkeypatch, tmp_path):
    from nh_workbench import ops, skill

    loaded(window)
    tools = gui.VIEW_KEYS.index("tools")
    assert window.tabs.tabText(tools) == "Tools"
    window.tabs.setCurrentIndex(tools)
    assert [window.tools.proxy.index(r, 0).data() for r in range(5)][:2] == ["spark profiles", "Clone missing repos"]
    window.tools.selectRow(2)
    assert window._selected_tool() == "jars" and "Closed-source mods in the pack: 1" in window.detail.toPlainText()

    downloads = []

    async def fake_download(entries, folder):
        downloads.append(folder)
        return pack.JarReport(downloaded=["Thaumcraft.jar"])

    monkeypatch.setattr(pack, "download_closed", fake_download)
    window.tools.activated.emit(window.tools.currentIndex())  # Enter runs the tool, as in the TUI
    wait_for(lambda: downloads and not window.running)
    assert "1 downloaded" in window.message.text()

    jar = tmp_path / "built.jar"
    jar.write_bytes(b"x" * 2048)
    monkeypatch.setattr(ops, "built_jars", lambda states, org: [(window.model.rows[0], [jar])])
    asked = []
    monkeypatch.setattr(window, "_confirm", lambda title, text, details="": asked.append(title) or True)
    window.tools.selectRow(3)
    window.tools.activated.emit(window.tools.currentIndex())
    wait_for(lambda: not jar.exists() and not window.running)
    assert asked and asked[0].startswith("Delete 1 built jars")

    installs = []
    monkeypatch.setattr(skill, "install", lambda dest, agent: installs.append(agent) or (tmp_path / agent, None))
    window.tools.selectRow(4)
    QTimer.singleShot(200, lambda: QTest.keyClick(QApplication.activePopupWidget(), Qt.Key.Key_1))
    window.tools.activated.emit(window.tools.currentIndex())
    wait_for(lambda: installs and not window.running)
    assert installs == [next(iter(skill.AGENTS))] and "Skill installed" in window.message.text()


def test_spark_window(window, monkeypatch):
    from nh_workbench import spark

    asked = []
    monkeypatch.setattr(spark, "load", lambda source, refresh=False: (object(), "abc"))
    monkeypatch.setattr(spark, "summary", lambda p, name, thread, windows, *a: "summary\n  12.5%  1.2s  a.B.tick")
    monkeypatch.setattr(
        spark, "focus_report", lambda p, name, method, up, *a: asked.append((method, up)) or f"report of {method}"
    )
    window._open_tool("spark")
    sw = window.spark_window
    try:
        sw.source.setEditText("abc")
        sw.load()
        wait_for(lambda: sw.text.startswith("summary"))
        sw.report.show_text(sw.text, 1)  # the cursor on the method line
        sw.report.setFocus()
        QTest.keyClick(sw.report, Qt.Key.Key_Return)  # what it calls
        wait_for(lambda: sw.text == "report of a.B.tick")
        assert asked == [("a.B.tick", False)] and "calls of a.B.tick" in sw.status.text()
        sw.back()
        wait_for(lambda: sw.text.startswith("summary"))
        sw.report.show_text(sw.text, 1)
        QTest.keyClick(sw.report, Qt.Key.Key_C)  # who calls it
        wait_for(lambda: len(asked) == 2)
        assert asked[1] == ("a.B.tick", True)
    finally:
        sw.close()
