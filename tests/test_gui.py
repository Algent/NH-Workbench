import os
import time

import pytest

pytest.importorskip("PySide6", reason="the desktop UI is an optional extra: uv sync --extra desktop")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from rich.text import Text  # noqa: E402

from nh_workbench import github, gui, pack  # noqa: E402
from nh_workbench.models import PackEntry, PackInfo  # noqa: E402
from nh_workbench.proc import Result  # noqa: E402


@pytest.fixture
def window(sandbox, monkeypatch):
    async def no_prs(states):
        return github.PrData()

    async def fake_pack(cfg, force=False):
        closed = PackEntry("Thaumcraft", "external", "4.2", closed=True)
        return PackInfo(entries=[closed, PackEntry("Missing", "github", "1", repo="o/m")], release="2.9.0")

    monkeypatch.setattr(github, "fetch_prs", no_prs)
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
    window.tabs.setCurrentIndex(0)
    assert "clone" not in shown()
