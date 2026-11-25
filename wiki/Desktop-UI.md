# Desktop UI

The same tabs, data and actions as the [terminal UI](Terminal-UI), in a window: Qt (PySide6), sortable
columns, resizable panes, right-click menus, and Textual's themes.

![The desktop UI on the All tab, with Textual's dark theme](images/desktop-ui.png)

## Starting it

It needs Qt, an optional extra of about 100 MB:

```
uv run --extra desktop nh-workbench gui
```

Once installed (`uv sync --extra desktop`, or `uv tool install -e ".[desktop]"`), `nh-workbench gui` (or
`nhwb gui`) opens it, and so does `nh-workbench-desktop`, which on Windows starts without a console window: point
a shortcut at it (`.venv\Scripts\nh-workbench-desktop.exe` in a checkout). `--root PATH` works as for the other
commands.

## Tabs and the detail pane

The tabs are the terminal UI's, with the same counts and filters: see its [tabs](Terminal-UI#tabs). The pane
under the list shows the selected line's details, as in the terminal UI, and can be resized by dragging the
divider.

Columns size themselves to their contents until you drag one. Clicking a header sorts by that column.

## Actions

- **Toolbar**: the actions of the current tab, the same set the terminal UI's footer shows.
- **Menu of a line**: `Enter`, double click or right click. On a repo: fetch, pull, switch to main, delete
  merged branches, protect branches, open in IntelliJ, on GitHub or in the file manager, set the main branch or
  group, hide the repo. On a pack mod or a PR: clone, check out, open.
- **Cleanup** and **To main** list what they will do and ask first; `No` is the default.
- Tools tab: `Enter` on a tool runs it. spark profiles open in a window of their own.

## Keys

The terminal UI's [keys](Terminal-UI#keys) work the same way, on the same tabs: `f`, `p`, `F`, `P`, `r`, `m`,
`d`, `o`, `w`, `c`, `t`, `v`, `s`, `/`, `Tab`, `x`, `q`. The menus answer to their letters too. Besides those:

| Key | Action |
|---|---|
| `Ctrl+F` | Filter by name (`Esc` clears it, `Enter` goes back to the list) |
| `F5` | Rescan, or refresh from GitHub on the PR tabs |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | Next / previous tab |
| `Ctrl+,` | Settings |
| `Ctrl+Q` | Quit |

Each toolbar button's tooltip gives its letter and its `Ctrl+Shift` shortcut.

## Settings

The cog at the end of the toolbar, `x` or `Ctrl+,`: theme, text size, repo folder, clone over SSH or HTTPS,
auto-refresh interval and whether it fetches, IntelliJ launcher (or none), hidden repos and protected branches.
Save writes them to the config file, keeping its comments. Its buttons download the pack data again, open the
config or cache folder, and update the tool itself (as in the terminal UI, a message says when its checkout has
new commits).

## Themes

Textual's themes, the ones the terminal UI offers: Textual dark (the default), Dracula, Nord, Gruvbox,
Catppuccin, Tokyo night, Solarized, Rose pine and more, light ones included, or the system's own colours. The
theme is one setting for both UIs: picking one in either applies to the other at its next start. See
[Configuration](Configuration) for `theme` and `font_size`.
