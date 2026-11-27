"""Rebindable keys, shared by both UIs.

A letter is a single typed character: the terminal UI's key, and the desktop UI's on the list. A shortcut is the
desktop UI's toolbar key, as Qt writes it (Ctrl+Shift+F). Overrides live in the config's [keys] and [shortcuts].
"""

from dataclasses import dataclass

from .config import Config
from .views import VIEWS, action_shown


@dataclass(frozen=True)
class KeyAction:
    id: str  # also the terminal UI's binding id and the desktop UI's toolbar action
    label: str
    letter: str
    shortcut: str = ""  # "": none in the desktop UI
    shown_as: str = ""  # the action action_shown() knows it by, when not its id; "-": shown on every tab
    screen: str = ""  # "": the main window's tabs; "spark": the spark tool, whose keys clash only among themselves

    @property
    def shown(self) -> str:
        return self.shown_as or self.id


ACTIONS = [
    KeyAction("fetch", "Fetch all", "f", "Ctrl+Shift+F"),
    KeyAction("fetch_one", "Fetch the selected repo", "F", shown_as="fetch"),
    KeyAction("pull", "Pull safe", "p", "Ctrl+Shift+P"),
    KeyAction("pull_one", "Pull the selected repo", "P", shown_as="pull"),
    KeyAction("to_main", "To main", "m", "Ctrl+Shift+M"),
    KeyAction("cleanup", "Cleanup", "d", "Ctrl+Shift+Del"),
    KeyAction("rescan", "Rescan", "r", "F5"),
    KeyAction("reload_prs", "Refresh from GitHub (My PRs, Reviews)", "r", "F5"),
    KeyAction("reload_org", "Search again (All PRs)", "r", "F5"),
    KeyAction("clone", "Clone (Pack)", "c", "Ctrl+Shift+C"),
    KeyAction("checkout", "Check out (PR tabs)", "c", "Ctrl+Shift+K"),
    KeyAction("toggle_team", "Team reviews (Reviews)", "t", "Ctrl+Shift+T"),
    KeyAction("toggle_drafts", "Drafts (All PRs)", "t", "Ctrl+Shift+D"),
    KeyAction("pr_filters", "Show only (All PRs)", "v", "Ctrl+Shift+V"),
    KeyAction("open_web", "Open in browser", "w", "Ctrl+Shift+G"),
    KeyAction("open_ide", "Open in IntelliJ", "o", "Ctrl+Shift+O"),
    KeyAction("open_files", "Open in file manager", "e", "Ctrl+Shift+E"),
    KeyAction("open_terminal", "Open terminal here", "T", "Ctrl+Alt+T"),
    KeyAction("filter", "Filter", "/", "Ctrl+F"),
    KeyAction("sort", "Sort", "s"),
    KeyAction("extras", "Settings", "x", "Ctrl+,", shown_as="-"),
    KeyAction("open_config", "Open config folder", "C", shown_as="-"),
    KeyAction("open_logs", "Open logs folder", "L", shown_as="-"),
    KeyAction("quit", "Quit", "q", "Ctrl+Q", shown_as="-"),
    # The spark tool: the desktop window's text report and call tree use the first four, the terminal UI all.
    KeyAction("spark_calls", "spark: what the method calls", "t", screen="spark"),
    KeyAction("spark_callers", "spark: who calls the method", "c", screen="spark"),
    KeyAction("spark_summary", "spark: back to the summary", "s", screen="spark"),
    KeyAction("spark_copy", "spark: copy the report for an LLM", "y", screen="spark"),
    KeyAction("spark_window", "spark: pick a minute (terminal UI)", "w", screen="spark"),
    KeyAction("spark_thread", "spark: pick a thread (terminal UI)", "h", screen="spark"),
    KeyAction("spark_save", "spark: save the report (terminal UI)", "e", screen="spark"),
    KeyAction("spark_open", "spark: open another profile (terminal UI)", "o", screen="spark"),
    KeyAction("spark_reload", "spark: download again (terminal UI)", "r", screen="spark"),
    KeyAction("spark_compare", "spark: compare heap summaries (terminal UI)", "m", screen="spark"),
    KeyAction("spark_leave", "spark: close (terminal UI)", "q", screen="spark"),
]
BY_ID = {a.id: a for a in ACTIONS}
GROUP_TITLES = {"": "Main window", "spark": "spark tool"}  # screen -> the title the Keys lists show it under


def grouped() -> list[tuple[str, list[KeyAction]]]:
    """The actions under their group's title, for the Keys lists of both UIs."""
    return [(title, [a for a in ACTIONS if a.screen == screen]) for screen, title in GROUP_TITLES.items()]


def short_label(action: KeyAction) -> str:
    """The label under its group's title: "what the method calls" rather than "spark: what the method calls"."""
    prefix = f"{action.screen}: "
    label = action.label.removeprefix(prefix) if action.screen else action.label
    return label[:1].upper() + label[1:]


DEFAULT_LETTERS = {a.id: a.letter for a in ACTIONS}
# Taken by keys that cannot be rebound: Tab and Esc in both UIs, Ctrl+Tab to change tabs in the desktop UI.
FIXED_SHORTCUTS = {"Ctrl+Tab", "Ctrl+Shift+Tab", "Tab", "Shift+Tab", "Esc", "Ctrl+C"}


def letters(cfg: Config) -> dict[str, str]:
    """Each action's letter, the config's overrides on top of the defaults."""
    return {a.id: k if (k := cfg.keys.get(a.id)) and not letter_error(k) else a.letter for a in ACTIONS}


def shortcuts(cfg: Config) -> dict[str, str]:
    """Each action's desktop shortcut ("" for none)."""
    return {a.id: cfg.shortcuts.get(a.id, a.shortcut) for a in ACTIONS}


def letter_error(letter: str) -> str | None:
    if len(letter) != 1 or not letter.isprintable() or letter.isspace():
        return f"Not a single key: {letter!r}"
    return None


def _views_of(action: KeyAction) -> set[str]:
    if action.screen:
        return {action.screen}
    return {view for view in VIEWS if action.shown == "-" or action_shown(action.shown, view)}


def clashes(mapping: dict[str, str]) -> list[tuple[str, str]]:
    """Pairs of action ids that share a key on some tab. Actions never on the same tab may share one (r, c, t)."""
    found = []
    taken = [(a, mapping.get(a.id, "")) for a in ACTIONS]
    for i, (a, key) in enumerate(taken):
        for b, other in taken[i + 1 :]:
            if same_key(key, other) and _views_of(a) & _views_of(b):
                found.append((a.id, b.id))
    return found


def same_key(a: str, b: str) -> bool:
    """Shortcuts compare without regard to case; letters do not (f and F are different keys)."""
    if not a or not b:
        return False
    return a.lower() == b.lower() if len(a) > 1 and len(b) > 1 else a == b


def clash_text(mapping: dict[str, str], action_id: str) -> str | None:
    """Why action_id's key in mapping cannot be used, or None."""
    for a, b in clashes(mapping):
        if action_id in (a, b):
            other = BY_ID[b if a == action_id else a]
            return f"{mapping[action_id]} is already {other.label}"
    return None


def hint(cfg: Config, action_id: str) -> str:
    """The letter for a message, as in "Settings (x)"."""
    return letters(cfg)[action_id]
