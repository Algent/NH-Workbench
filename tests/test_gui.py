import os
import time
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("PySide6", reason="the desktop UI is an optional extra: uv sync --extra desktop")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
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
    win = gui.MainWindow(sandbox.cfg, sandbox.root)  # a reloaded config keeps to the sandbox
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
    window.select_view("pack")
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

    assert shown() == {"fetch", "pull", "to_main", "cleanup", "rescan", "open_web", "open_ide"}
    window.select_view("pack")
    assert shown() == {"rescan", "clone", "open_web", "open_ide"}  # what the TUI's footer shows on Pack
    window.select_view("mine")
    assert shown() == {"reload_prs", "open_web", "checkout"}  # no IntelliJ: o/m is not checked out here
    window.select_view("reviews")
    assert shown() == {"reload_prs", "open_web", "checkout", "toggle_team"}
    window.select_view("org")
    assert shown() == {"reload_org", "open_web", "checkout", "toggle_drafts", "pr_filters"}
    window.select_view("tools")
    assert shown() == {"rescan"} and not window.filter_action.isVisible()  # as the TUI's footer on Tools
    window.select_view("all")
    assert "clone" not in shown() and "toggle_team" not in shown()


def test_my_prs_tab_lists_and_opens(window, monkeypatch):
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", opened.append)
    loaded(window)
    wait_for(lambda: window.mine.source.rows)
    window.select_view("mine")
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
    window.select_view("mine")
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
    window.select_view("reviews")
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
    window.select_view("mine")
    window.mine.setFocus()
    QTest.keyClick(window.mine, Qt.Key.Key_R)  # as in the terminal UI: r refreshes the PRs, not the repos
    wait_for(lambda: calls and not window.running)
    assert window.mine.source.rows and window.message.text() != "Scanning..."


def test_all_prs_tab(window, monkeypatch, tmp_path):
    from nh_workbench import config

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    loaded(window)
    wait_for(lambda: window.mine.source.rows and window.reviews.source.rows)
    org_tab = window.tab_index("org")
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
    tools = window.tab_index("tools")
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


def test_updates_keep_the_selected_line(window):
    loaded(window)
    window.select_view("pack")
    window.pack.selectRow(window.pack.proxy.rowCount() - 1)
    assert window.pack.current().name == "Thaumcraft"
    closed = PackEntry("Thaumcraft", "external", "4.3", closed=True)
    window.pack.source.set_rows([PackEntry("Aaa", "github", "1", repo="o/a"), closed])  # one new, one gone
    assert [window.pack.proxy.index(r, 0).data() for r in range(2)] == ["Aaa", "Thaumcraft"]
    assert window.pack.current() is closed


def test_end_of_a_batch_keeps_a_newer_message(window):
    loaded(window)
    window._progress("Fetching", 0, 1)
    window._notify("Searching all open PRs of o, this takes a while...")
    window._progress("", 0, 0)
    assert window.message.text().startswith("Searching")
    window._progress("Fetching", 0, 1)
    window._progress("", 0, 0)
    assert window.message.text() == "1 repos"


def test_closing_the_window_closes_the_spark_window(window):
    window._open_tool("spark")
    assert window.spark_window.isVisible()
    window.close()
    assert not window.spark_window.isVisible()


def test_run_saves_the_picked_folder(sandbox, monkeypatch):
    saved = []

    class App:
        def setApplicationName(self, name): ...
        def setStyle(self, style): ...
        def setPalette(self, palette): ...
        def setStyleSheet(self, sheet): ...
        def font(self):
            return QApplication.font()

        def setFont(self, font): ...
        def exec(self):
            return 0

    class Window:
        def __init__(self, cfg, root_override=None): ...
        def show(self): ...

    monkeypatch.setattr(gui, "QApplication", type("Qa", (), {"instance": staticmethod(App)}))
    monkeypatch.setattr(gui.config, "suggest_root", lambda: None)  # nothing to offer: straight to the folder picker
    monkeypatch.setattr(gui, "setup_logging", lambda verbose=False: None)
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *a: str(sandbox.root))
    monkeypatch.setattr(gui, "MainWindow", Window)
    monkeypatch.setattr(gui.config, "save_setting", lambda keys, value: saved.append((keys, value)))
    cfg = gui.Config(root=sandbox.root / "missing")
    assert gui.run(cfg) == 0
    assert cfg.root == sandbox.root and saved == [(("root",), str(sandbox.root))]


def test_first_start_offers_the_likely_folder(sandbox, monkeypatch, tmp_path):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui.config, "suggest_root", lambda: sandbox.root)
    picked = []
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *a: picked.append(a[2]) or str(tmp_path))

    def answer(button: str):
        def press(box) -> int:
            chosen = box.defaultButton() if button == "use" else next(b for b in box.buttons() if b.text() == button)
            chosen.click()
            return 0

        monkeypatch.setattr(gui.QMessageBox, "exec", press)

    answer("use")
    assert gui.ask_root(tmp_path / "missing") == sandbox.root
    answer("Choose another...")
    assert gui.ask_root(tmp_path / "missing") == tmp_path and picked == [str(sandbox.root)]  # opens at the guess
    answer("Cancel")
    assert gui.ask_root(tmp_path / "missing") is None


def test_desktop_launcher_moves_the_old_dirs(sandbox, monkeypatch):
    refreshed = []
    monkeypatch.setattr(gui.config, "move_old_dirs", lambda: [("old", "new")])
    monkeypatch.setattr(gui.skill, "refresh_installed", lambda: refreshed.append(True))
    monkeypatch.setattr(gui.config, "load", lambda: sandbox.cfg)
    monkeypatch.setattr(gui, "run", lambda cfg: 0)
    with pytest.raises(SystemExit):
        gui.main()
    assert refreshed == [True]


def test_settings_dialog_saves_and_reloads(window, sandbox, monkeypatch, tmp_path):
    from nh_workbench import config
    from nh_workbench.gui_settings import SettingsDialog

    path = tmp_path / "config.toml"
    path.write_text(
        f"root = '{sandbox.root.as_posix()}'\n[repos.Hidden]\nignore = true\n[repos.repo]\nkeep_branches = ['a', 'b']\n"
    )
    monkeypatch.setattr(config, "config_path", lambda: path)
    window.cfg = window.wb.cfg = config.load()
    loaded(window)

    dialog = SettingsDialog(window)
    dialog.minutes.setValue(5)
    dialog.protocol.setCurrentIndex(dialog.protocol.findData("https"))
    dialog.ide_off.setChecked(True)
    dialog.hidden.takeItem(0)
    dialog.kept.takeItem(0)  # repo  a
    dialog.accept()
    wait_for(lambda: not window.running)
    cfg = config.load()
    assert (cfg.refresh_minutes, cfg.clone_protocol, cfg.ide_off) == (5, "https", True)
    assert cfg.hidden == [] and cfg.kept == [("repo", "b")]
    assert window.cfg.ide_off and not window.tab_actions["open_ide"].isVisible()

    errors = []
    dialog = SettingsDialog(window)
    monkeypatch.setattr(dialog, "_error", errors.append)
    dialog.root.setText(str(tmp_path / "nope"))
    assert dialog.changes() is None and errors[0].startswith("Not a folder")


def test_auto_refresh(window):
    loaded(window)
    window.cfg.refresh_minutes, window.cfg.refresh_fetch = 1, True
    window._auto_refresh()
    assert not window.running  # not yet a minute since the last scan
    window.last_loaded -= 61
    window._auto_refresh()
    assert window.running == "Fetching"
    wait_for(lambda: not window.running)


def test_to_main_and_cleanup(window, sandbox, monkeypatch, tmp_path):
    from nh_workbench import config

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    loaded(window)
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
    monkeypatch.setattr(gui.ConfirmDialog, "exec", lambda self: True)
    window._rescan()
    wait_for(lambda: not window.running and window.model.rows[0].current and window.model.rows[0].current.pr)
    assert window.model.rows[0].head == "algent/two"

    window._to_main_all()
    wait_for(lambda: window.message.text().startswith("Switched"))
    assert window.model.rows[0].head == "master"

    window._cleanup(list(window.model.rows))
    wait_for(lambda: window.message.text().startswith("Deleted"))
    assert [b.name for b in window.model.rows[0].branches] == ["master"]
    assert "algent/one" in (tmp_path / "cache" / "nh-workbench" / "deleted-branches.log").read_text()


def test_repo_menu_settings(window, sandbox, monkeypatch, tmp_path):
    from nh_workbench import config

    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: path)
    loaded(window)
    st = window.model.rows[0]
    answers = iter([("master", True), ("extra", True)])
    monkeypatch.setattr(gui.QInputDialog, "getItem", lambda *a: next(answers))
    window._repo_action("main_branch", st)
    wait_for(lambda: not window.running)
    window._repo_action("group", window.model.rows[0])
    wait_for(lambda: not window.running and window.model.rows[0].group == "extra")
    assert config.load().repo("repo") == config.RepoConfig(tracked_branch="master", group="extra")

    dialog = gui.KeepDialog(window, window.model.rows[0], ("gone",))
    assert [dialog.list.item(i).text() for i in range(dialog.list.count())] == ["master", "gone  (no local branch)"]
    dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
    assert dialog.chosen() == ["master", "gone"]

    monkeypatch.setattr(window, "_confirm", lambda *a: True)
    window._repo_action("hide", window.model.rows[0])
    wait_for(lambda: not window.running and not window.model.rows)
    assert config.load().hidden == ["repo"]


def test_terminal_ui_keys_on_every_tab(window, sandbox, monkeypatch):
    loaded(window)
    window.show()  # focus moves only in a shown, active window
    window.activateWindow()
    wait_for(window.isActiveWindow)
    window.repos.setFocus()
    tip = sandbox.push_upstream("second")
    QTest.keyClick(window.repos, Qt.Key.Key_F)  # f: fetch all
    assert window.running == "Fetching"
    wait_for(lambda: not window.running)
    QTest.keyClick(window.repos, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)  # P: pull the selected repo
    wait_for(lambda: not window.running and sandbox.sha(sandbox.work) == tip)

    QTest.keyClick(window.repos, Qt.Key.Key_S)  # s: the next sort, as the TUI's
    assert window.message.text() == "Sorted by last commit"
    assert window.repos.horizontalHeader().sortIndicatorSection() == gui.REPO_COLUMNS.index("Last commit")

    QTest.keyClick(window.repos, Qt.Key.Key_Slash)
    assert window.filter.hasFocus()
    QTest.keyClicks(window.filter, "zzz")
    assert window.repos.proxy.rowCount() == 0
    QTest.keyClick(window.filter, Qt.Key.Key_Escape)  # clears it and goes back to the list
    assert window.filter.text() == "" and window.repos.proxy.rowCount() == 1

    QTest.keyClick(window.repos, Qt.Key.Key_Tab)  # the next tab
    assert window._view() == gui.VIEW_KEYS[1]
    QTest.keyClick(window.table, Qt.Key.Key_Backtab)
    assert window._view() == gui.VIEW_KEYS[0]

    window.select_view("mine")
    QTest.keyClick(window.mine, Qt.Key.Key_S)
    assert window.message.text() == "Sorted by smallest first"

    opened = []
    monkeypatch.setattr(window, "open_settings", lambda: opened.append("settings"))
    QTest.keyClick(window.mine, Qt.Key.Key_X)
    assert opened == ["settings"]


def test_themes(window, sandbox, monkeypatch, tmp_path):
    from PySide6.QtGui import QPalette

    from nh_workbench import config, gui_theme
    from nh_workbench.gui_settings import SettingsDialog

    path = tmp_path / "config.toml"
    path.write_text(f"root = '{sandbox.root.as_posix()}'\n")
    monkeypatch.setattr(config, "config_path", lambda: path)
    assert gui_theme.theme_name("") == "textual-dark" and gui_theme.theme_name("nope") == "textual-dark"
    loaded(window)
    try:
        window.cfg.theme = "dracula"  # as the config file says once picked in Settings
        window.set_theme("dracula")
        assert QApplication.palette().color(QPalette.ColorRole.Base).name() == "#2b2e3b"
        assert any("#50fa7b" in rich for _, rich in window.model._cells[0])  # Dracula's green in the table
        window._notify("oops", True)
        assert "#fe5555" in window.message.styleSheet().lower()

        dialog = SettingsDialog(window)
        assert dialog.theme.currentData() == "dracula"
        dialog.theme.setCurrentIndex(dialog.theme.findData("textual-dark"))
        dialog.accept()
        assert window.theme_name == "textual-dark" and config.load().theme == "textual-dark"

        dialog = SettingsDialog(window)
        assert dialog.theme.findData("nord") >= 0 and dialog.theme.findData("ansi-dark") < 0  # ANSI: terminal colours
        dialog.font_size.setValue(14)
        dialog.accept()
        assert config.load().font_size == 14 and window.repos.font().pointSizeF() == 14
        assert window.repos.verticalHeader().defaultSectionSize() == gui_theme.row_height(14)
    finally:
        window.set_theme(gui_theme.SYSTEM)  # the palette belongs to the whole app: leave it as the next tests expect
        QApplication.instance().setStyleSheet("")


def test_cells_are_not_cut_short(window):
    loaded(window)
    st = window.model.rows[0]
    st.subject = "x" * 120
    window.model.update_row(st)
    assert "x" * 120 in window.model._cells[0][gui.REPO_COLUMNS.index("Last commit")][0]


def test_message_history(window, monkeypatch):
    loaded(window)
    window._progress("Fetching", 0, 3)  # passing state: shown, not kept
    window._notify("first")
    window._notify("it broke", True)
    window._notify("last")
    assert [text for _, text, _ in window.history][-3:] == ["first", "it broke", "last"]
    assert "Fetching..." not in [text for _, text, _ in window.history]
    assert window.history_button.text() == "History  ⚠ 1"

    window._show_history()
    popup = window.history_popup
    assert [popup.item(i).text().split("  ", 1)[1] for i in range(3)] == ["last", "it broke", "first"]  # newest first
    assert window.history_button.text() == "History"  # seen
    popup.item(1).setSelected(True)
    QTest.keySequence(popup, QKeySequence.StandardKey.Copy)
    assert QApplication.clipboard().text().endswith("it broke")
    popup.close()

    for i in range(gui.HISTORY_SIZE + 5):
        window._notify(f"m{i}")
    assert len(window.history) == gui.HISTORY_SIZE and window.history[-1][1] == f"m{gui.HISTORY_SIZE + 4}"


def test_a_job_of_several_steps_ends_once(window):
    from nh_workbench import ops

    loaded(window)
    ends = []
    progress = window.wb.on_progress
    window.wb.on_progress = lambda label, done, total: (
        ends.append(label) if not label else None,
        progress(label, done, total),
    )

    async def scan() -> None:
        await window.wb.batch("Scanning", lambda p: ops.scan_repo(window.cfg, p))

    async def two_steps() -> None:  # as a cleanup: a switch, then a scan
        async with window.wb.job():
            await scan()
            assert ends == []  # the first step did not hand the toolbar back
            await scan()

    window.loop.submit(two_steps()).result(20)
    assert ends == [""]
    window.loop.submit(scan()).result(20)  # a batch on its own still ends its job
    assert ends == ["", ""]


def test_gui_log(monkeypatch):
    import logging
    import sys

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # put back after the test
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: None)  # no traceback in the test output
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = gui.setup_logging()
        logging.getLogger("nh_workbench.test").info("not kept without -v")
        logging.getLogger("nh_workbench.test").warning("kept")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        for handler in root.handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
        assert "kept" in text and "not kept" not in text
        assert "unhandled error" in text and "RuntimeError: boom" in text
    finally:
        for handler in set(root.handlers) - set(before):
            root.removeHandler(handler)
            handler.close()


def test_settings_shows_the_pack_data(window):
    from nh_workbench.gui_settings import SettingsDialog

    loaded(window)
    labels = [label.text() for label in SettingsDialog(window).findChildren(gui.QLabel)]
    assert any("Release" in text and "2.9.0" in text for text in labels)
