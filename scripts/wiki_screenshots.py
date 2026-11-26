"""Retake the wiki screenshots in wiki/images, on your own checkouts, with nothing opening on screen.

    uv run python scripts/wiki_screenshots.py [desktop] [terminal] [spark] [--repo GT5-Unofficial] [--spark CODE]

With no names, all three. Both UIs start as they do for you (a scan, GitHub and pack data, no fetch) with
Textual's dark theme, which is not saved. Paths under your home folder read C:\\Users\\Player instead (same
length, so the terminal grid does not shift). The terminal UI is drawn cell by cell into a PNG: an SVG renders
differently from one viewer to the next.
"""

import argparse
import asyncio
import dataclasses
import io
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")  # offscreen Qt finds no fonts by itself

from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from nh_workbench import config, gui, gui_theme, spark

IMAGES = Path(__file__).resolve().parent.parent / "wiki" / "images"
THEME = "textual-dark"
HOME = str(Path.home())
MASK = str(Path.home().parent / "Player")


def masked(text: str) -> str:
    return text.replace(HOME, MASK).replace(HOME.replace("\\", "/"), MASK.replace("\\", "/"))


def settle(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.03)


def setup(cfg: config.Config) -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI"))
    gui_theme.apply(app, THEME, cfg.font_size)
    return app


def unhover(app: QApplication, window: QWidget) -> None:
    """Move the mouse off the window, so no button is drawn as hovered."""
    QCursor.setPos(4000, 4000)
    for widget in window.findChildren(QWidget):
        if widget.underMouse():
            QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    settle(app, 0.3)


def desktop(cfg: config.Config, repo: str) -> None:
    for name in ("rich_html", "text_html"):  # the details pane and the table cells
        render = getattr(gui, name)
        setattr(gui, name, lambda *a, _render=render, **k: masked(_render(*a, **k)))
    app = setup(cfg)
    window = gui.MainWindow(cfg)
    window.resize(1440, 860)
    window.show()
    end = time.monotonic() + 180
    settle(app, 2)
    while time.monotonic() < end and (window.running or not window.model.rowCount()):
        settle(app, 0.2)
    settle(app, 5)  # pull requests and pack versions land after the scan
    table = window.repos
    names = [str(table.proxy.index(r, 0).data()) for r in range(table.proxy.rowCount())]
    row = next((i for i, n in enumerate(names) if n == repo), 0)
    table.selectRow(row)
    table.scrollTo(table.proxy.index(row, 0))
    window.detail.setFocus()  # an unfocused list draws its selection dark, as in the old screenshot
    unhover(app, window)
    window.grab().save(str(IMAGES / "desktop-ui.png"))
    print(f"desktop-ui.png: {len(names)} repos, {names[row] if names else '-'} selected")
    window.hide()


def terminal(cfg: config.Config, repo: str, cols: int = 170, rows: int = 42) -> None:
    from rich.cells import cell_len
    from rich.color import blend_rgb
    from rich.console import Console
    from rich.terminal_theme import SVG_EXPORT_THEME
    from textual.widgets import DataTable

    from nh_workbench.tui import RepoApp

    async def capture() -> list:
        app = RepoApp(cfg)
        async with app.run_test(size=(cols, rows)) as pilot:
            for _ in range(3):
                await pilot.pause()
                await app.workers.wait_for_complete()
            await pilot.pause(1.0)
            table = app.query_one("#repos", DataTable)
            keys = [str(k.value) for k in table.rows]
            row = next((i for i, k in enumerate(keys) if Path(k).name == repo), 0)
            table.move_cursor(row=row)
            await pilot.pause(0.5)
            console = Console(
                width=cols, height=rows, file=io.StringIO(), force_terminal=True, color_system="truecolor",
                record=True, legacy_windows=False, safe_box=False,
            )  # fmt: skip
            console.print(app.screen._compositor.render_update(full=True, screen_stack=app._background_screens))
            print(f"terminal-ui.png: {len(keys)} repos, {Path(keys[row]).name if keys else '-'} selected")
            return list(console._record_buffer)

    segments = [s for s in asyncio.run(capture()) if not s.control]
    QApplication.instance() or QApplication(sys.argv)
    theme, base = SVG_EXPORT_THEME, SVG_EXPORT_THEME.background_color
    font = QFont("Cascadia Mono" if sys.platform == "win32" else "monospace")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(16)
    bold = QFont(font)
    bold.setBold(True)
    metrics = QFontMetrics(font)
    cw, ch, pad = metrics.horizontalAdvance("M"), metrics.height(), 14
    image = QImage(cols * cw + 2 * pad, rows * ch + 2 * pad, QImage.Format.Format_RGB32)
    image.fill(QColor(*base))
    p = QPainter(image)
    x = y = 0
    for seg in segments:  # every character in its own cell: glyphs from fallback fonts cannot shift the grid
        style = seg.style
        fg = style.color.get_truecolor(theme, True) if style and style.color else theme.foreground_color
        bg = style.bgcolor.get_truecolor(theme, False) if style and style.bgcolor else base
        if style and style.reverse:
            fg, bg = bg, fg
        if style and style.dim:
            fg = blend_rgb(fg, bg, 0.4)
        p.setFont(bold if style and style.bold else font)
        for c in masked(seg.text):
            if c == "\n":
                x, y = 0, y + 1
                continue
            width = cell_len(c) or 1
            left, top = pad + x * cw, pad + y * ch
            p.fillRect(left, top, width * cw, ch, QColor(*bg))
            if not c.isspace():
                p.setPen(QColor(*fg))
                p.drawText(left, top + metrics.ascent(), c)
            x += width
    p.end()
    image.save(str(IMAGES / "terminal-ui.png"))


def spark_window(cfg: config.Config, code: str) -> None:
    from nh_workbench.gui_spark import SparkWindow, digest

    app = setup(cfg)

    class Owner(QWidget):
        """What the spark window needs of the main window."""

        theme_name = THEME

        def __init__(self) -> None:
            super().__init__()
            self.cfg = cfg

        def warning_color(self) -> str:
            return gui_theme.warning_color(THEME)

    window = SparkWindow(Owner())
    upload, name = spark.load(code)
    if isinstance(upload, spark.Heap):
        raise SystemExit(f"{code} is a heap summary: pick a profile")
    window._loaded((upload, name, spark.summary(upload, name, None, None), digest(upload, None, None)))
    window.source.setEditText(f"https://spark.lucko.me/{code}")
    window.resize(1280, 1240)  # the whole overview, its machine details included
    window.show()
    window.timeline.setFocus()
    unhover(app, window)
    window.grab().save(str(IMAGES / "spark-overview.png"))
    window.resize(1280, 820)
    window.show_tab("calls")
    window.tree.setFocus()
    settle(app, 0.3)
    window.grab().save(str(IMAGES / "spark-call-tree.png"))
    print(f"spark-overview.png, spark-call-tree.png: {code}")
    window.hide()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    names = ["desktop", "terminal", "spark"]
    parser.add_argument("which", nargs="*", help=f"any of {', '.join(names)} (default: all)")
    parser.add_argument("--repo", default="GT5-Unofficial", help="the checkout to select in both UIs")
    parser.add_argument("--spark", default="pWtoY8WVPg", help="the spark profile to show (a code or a link)")
    args = parser.parse_args()
    if unknown := set(args.which) - set(names):
        parser.error(f"unknown: {', '.join(sorted(unknown))}")
    which = args.which or names
    cfg = dataclasses.replace(config.load(), theme=THEME)  # not saved
    if "spark" in which:
        spark_window(cfg, args.spark)
    if "desktop" in which:
        desktop(cfg, args.repo)
    if "terminal" in which:
        terminal(cfg, args.repo)
    sys.stdout.flush()
    os._exit(0)  # the desktop UI's background loop would keep the process alive


if __name__ == "__main__":
    main()
