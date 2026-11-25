"""The desktop UI's colours, taken from Textual's themes so it can look like the terminal UI.

The theme setting is shared with the terminal UI: one picked in either applies to both. "system" keeps Qt's own
colours (the terminal UI then shows its default).
"""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from textual.color import Color
from textual.theme import BUILTIN_THEMES

SYSTEM = "system"
DEFAULT = "textual-dark"  # the terminal UI's default too
THEMES = {"textual-dark": "Textual dark", "dracula": "Dracula", SYSTEM: "System colours"}  # offered in Settings
WARNING = "#c62828"  # warnings with the system colours


def theme_name(configured: str) -> str:
    """The theme to show: the configured one when it is known, else the default."""
    return configured if configured == SYSTEM or configured in BUILTIN_THEMES else DEFAULT


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
QHeaderView::section {{ background: {v["panel"]}; color: {v["foreground"]}; border: none; padding: 4px 6px;
    font-weight: 600; }}
QToolBar {{ background: {v["panel"]}; border: none; spacing: 2px; padding: 2px; }}
QStatusBar {{ background: {v["panel"]}; }}
QTableView, QTextBrowser, QPlainTextEdit {{ border: none; }}
QSplitter::handle {{ background: {v["panel"]}; }}
QProgressBar {{ background: {v["surface"]}; border: none; text-align: center; color: {v["foreground"]}; }}
QProgressBar::chunk {{ background: {v["primary"]}; }}
QToolTip {{ background: {v["panel"]}; color: {v["foreground"]}; border: 1px solid {v["primary"]}; }}
"""


def apply(app: QApplication, name: str) -> None:
    c = colors(name)
    if c is None:
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")
        return
    app.setPalette(palette(c))
    app.setStyleSheet(style_sheet(c))


def warning_color(name: str) -> str:
    c = colors(name)
    return c["error"].hex6 if c else WARNING
