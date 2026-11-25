"""The desktop UI's colours, taken from Textual's themes so it can look like the terminal UI.

The theme setting is shared with the terminal UI: one picked in either applies to both. "system" keeps Qt's own
colours (the terminal UI then shows its default).
"""

from PySide6.QtGui import QColor, QFontMetrics, QPalette
from PySide6.QtWidgets import QApplication
from textual.color import Color
from textual.theme import BUILTIN_THEMES

SYSTEM = "system"
DEFAULT = "textual-dark"  # the terminal UI's default too
# Offered in Settings: Textual's themes, but not the ANSI ones, made of the terminal's own colours.
THEMES = (
    {DEFAULT: "Textual dark"}
    | {name: name.replace("-", " ").capitalize() for name, theme in BUILTIN_THEMES.items() if not theme.ansi}
    | {SYSTEM: "System colours"}
)
WARNING = "#c62828"  # warnings with the system colours
FONT_SIZE = 11  # points, when the config sets none: Qt's own default (9 on Windows) reads small


def theme_name(configured: str) -> str:
    """The theme to show: the configured one when it is known, else the default."""
    return configured if configured in THEMES else DEFAULT


def colors(name: str) -> dict[str, Color] | None:
    """A Textual theme's colours, flattened to opaque ones; None for the system colours."""
    theme = BUILTIN_THEMES.get(name)
    if theme is None:
        return None
    made = theme.to_color_system().generate()
    background = Color.parse(made["background"])
    keys = ("surface", "panel", "primary", "secondary", "accent", "success", "warning", "error", "foreground")
    c = {"background": background} | {key: background + Color.parse(made[key]) for key in keys}
    c["zebra"] = c["surface"].blend(background + Color.parse(made["surface-lighten-1"]), 0.4)
    c["panel-light"] = background + Color.parse(made["panel-lighten-1"])
    c["muted"] = background.blend(c["foreground"], 0.6)
    c["disabled"] = background.blend(c["foreground"], 0.38)
    c["on-primary"] = c["primary"] + c["primary"].get_contrast_text(0.95)
    c["blurred"] = c["surface"].blend(c["primary"], 0.3)  # the terminal UI's cursor when the table has no focus
    return c


def palette(c: dict[str, Color]) -> QPalette:
    def q(key: str) -> QColor:
        return QColor(c[key].hex6)

    role, group = QPalette.ColorRole, QPalette.ColorGroup
    pal = QPalette()
    roles = {
        role.Window: "background",
        role.WindowText: "foreground",
        role.Base: "surface",
        role.AlternateBase: "zebra",
        role.Text: "foreground",
        role.Button: "panel",
        role.ButtonText: "foreground",
        role.BrightText: "error",
        role.Highlight: "primary",
        role.HighlightedText: "on-primary",
        role.ToolTipBase: "panel",
        role.ToolTipText: "foreground",
        role.PlaceholderText: "muted",
        role.Link: "accent",
        role.LinkVisited: "secondary",
        role.Light: "panel-light",
        role.Midlight: "panel",
        role.Mid: "zebra",
        role.Dark: "background",
        role.Shadow: "background",
        role.Accent: "primary",
    }
    for r, key in roles.items():
        pal.setColor(r, q(key))
    pal.setColor(group.Inactive, role.Highlight, q("blurred"))
    pal.setColor(group.Inactive, role.HighlightedText, q("foreground"))
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(group.Disabled, r, q("disabled"))
    pal.setColor(group.Disabled, role.Highlight, q("panel-light"))
    return pal


def style_sheet(c: dict[str, Color]) -> str:
    """What a palette cannot say: tabs underlined like Textual's, headers and bars on the panel colour."""
    v = {key: color.hex6 for key, color in c.items()}
    return f"""
QTabBar::tab {{ background: {v["background"]}; color: {v["muted"]}; padding: 6px 14px; border: none;
    border-bottom: 2px solid {v["background"]}; }}
QTabBar::tab:hover {{ color: {v["foreground"]}; background: {v["surface"]}; }}
QTabBar::tab:selected {{ color: {v["foreground"]}; border-bottom: 2px solid {v["primary"]}; }}
QTabBar::tab:disabled {{ color: {v["disabled"]}; padding: 6px 2px; background: {v["background"]}; }}
QHeaderView::section {{ background: {v["panel"]}; color: {v["foreground"]}; border: none; padding: 4px 6px;
    font-weight: 600; }}
QToolBar {{ background: {v["panel"]}; border: none; spacing: 2px; padding: 2px; }}
QStatusBar {{ background: {v["panel"]}; }}
QTableView, QTextBrowser, QPlainTextEdit {{ border: none; }}
QSplitter::handle {{ background: {v["panel"]}; }}
QProgressBar {{ background: {v["surface"]}; border: none; text-align: center; color: {v["foreground"]}; }}
QProgressBar::chunk {{ background: {v["primary"]}; }}
QToolTip {{ background: {v["panel"]}; color: {v["foreground"]}; border: 1px solid {v["primary"]}; }}
QListWidget#history {{ border: 1px solid {v["primary"]}; padding: 4px; }}
"""


def font_sheet(points: float) -> str:
    """The text size, in the style sheet: Windows gives item views and headers fonts of their own otherwise."""
    return f"""
QWidget {{ font-size: {points:g}pt; }}
QToolButton#cog {{ font-size: {points * 1.6:.1f}pt; }}
QLabel#title {{ font-size: {points * 1.15:.1f}pt; font-weight: 600; }}
QTabBar::tab:disabled {{ background: transparent; border: none; padding: 4px 2px; }}  /* the group dividers */
QToolButton#history-button::menu-indicator {{ image: none; width: 0; }}
"""


def apply(app: QApplication, name: str, size: int = 0) -> None:
    """Colours of the theme `name` and text of `size` points (0: the default) for the whole app."""
    points = size or FONT_SIZE
    font = app.font()
    font.setPointSizeF(points)
    app.setFont(font)
    c = colors(name)
    app.setPalette(palette(c) if c else app.style().standardPalette())
    app.setStyleSheet(font_sheet(points) + (style_sheet(c) if c else ""))


def row_height(size: int) -> int:
    font = QApplication.font()
    font.setPointSizeF(size or FONT_SIZE)
    return QFontMetrics(font).height() + 10


def warning_color(name: str) -> str:
    c = colors(name)
    return c["error"].hex6 if c else WARNING
