import os
import time

import pytest

pytest.importorskip("PySide6", reason="the desktop UI is an optional extra: uv sync --extra desktop")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from rich.text import Text  # noqa: E402

from nh_workbench import github, gui, pack  # noqa: E402
from nh_workbench.models import PackInfo  # noqa: E402


@pytest.fixture
def window(sandbox, monkeypatch):
    async def no_prs(states):
        return github.PrData()

    async def no_pack(cfg, force=False):
        return PackInfo(release="2.9.0")

    monkeypatch.setattr(github, "fetch_prs", no_prs)
    monkeypatch.setattr(pack, "load", no_pack)
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


def test_text_html_keeps_colors_and_escapes():
    app = QApplication.instance() or QApplication([])
    theme = gui.theme_for(app.palette())
    out = gui.text_html(Text.assemble(("a<b", "bold green"), " ", ("x", "dim")), theme)
    assert "a&lt;b" in out and "font-weight:600" in out and "color:#" in out


def test_window_lists_repos_and_fetches(window, sandbox):
    wait_for(lambda: window.model.rows and not window.wb.busy and window.wb.pack_info)
    assert [st.name for st in window.model.rows] == ["repo"]
    assert window.tabs.tabText(0).split() == ["All", "1"]
    assert "repo" in window.detail.toPlainText()

    window.filter.setText("nothing")
    assert window.proxy.rowCount() == 0
    window.filter.setText("")
    assert window.proxy.rowCount() == 1

    tip = sandbox.push_upstream("second")
    window.table.selectRow(0)
    window._repo_action("pull", window._selected())
    wait_for(lambda: window.model.rows[0].note is not None and not window.wb.busy)
    assert sandbox.sha(sandbox.work) == tip
