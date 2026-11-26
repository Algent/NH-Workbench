# Configuration

Everything is optional; see
[`config.example.toml`](https://github.com/Algent/NH-Workbench/blob/master/config.example.toml). Most settings
can also be changed from Settings (`x`) in the [Terminal UI](Terminal-UI), which keeps the file's comments.

## Where files are

| | Windows | Linux | macOS |
|---|---|---|---|
| Config | `%APPDATA%\nh-workbench\config.toml` | `~/.config/nh-workbench/config.toml` | `~/Library/Application Support/nh-workbench/config.toml` |
| Cache, logs | `%LOCALAPPDATA%\nh-workbench\Cache` | `~/.cache/nh-workbench` | `~/Library/Caches/nh-workbench` |

Linux follows `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` when set. Folders from before the rename
(`gtnh-devenv-tools`) are moved to these on the first start.

## Settings

| Setting | Default | |
|---|---|---|
| `root` | `~/IdeaProjects` | Folder holding the checkouts (also `--root PATH`) |
| `org` | `GTNewHorizons` | Its repos form the `gtnh` group; All PRs lists its PRs |
| `refresh_minutes` | `15` | The UI rescans this often; `0` turns it off |
| `clone_protocol` | `ssh` | `ssh` or `https`, for clones and the fork remotes PR checkout adds. Existing remotes are left alone |
| `theme` | `textual-dark` | The UI's theme; picking one in the command palette (`Ctrl+P`) or the desktop UI's Settings saves it here |
| `font_size` | `11` | Text size of the desktop UI, in points |
| `refresh_fetch` | `false` | Auto-refresh fetches every repo instead of only rescanning |
| `pr_filters` | none | All PRs filters, set with `v`: `local`, `pack`, `unreviewed`, `stale` |
| `concurrency` | `8` | Repos handled at the same time |
| `prune` | `true` | Drop stale remote-tracking refs on fetch |
| `ide_command` | found automatically | IntelliJ launcher; `false` hides the IDE actions |
| `cleanup_to_main` | `true` | Cleanup's "switch to main first" checkbox starts ticked |

## Keys

`[keys]` maps an action to its letter in both UIs, `[shortcuts]` to its desktop shortcut (`""` for none). Set from
Settings, Keys in either UI, which refuses clashes. Actions: `fetch`, `fetch_one`, `pull`, `pull_one`, `to_main`,
`cleanup`, `rescan`, `reload_prs`, `reload_org`, `clone`, `checkout`, `toggle_team`, `toggle_drafts`, `pr_filters`,
`open_web`, `open_ide`, `filter`, `sort`, `extras` (Settings), `open_config`, `open_logs`, `quit`.

The spark tool's keys clash only with each other: `spark_calls`, `spark_callers`, `spark_summary`, `spark_copy`
(both UIs), and in the terminal UI also `spark_window`, `spark_thread`, `spark_save`, `spark_open`,
`spark_reload`, `spark_compare`, `spark_leave`.

```toml
[keys]
fetch = "g"

[shortcuts]
fetch = "Ctrl+Alt+F"
quit = ""
```

## Per repo

Under `[repos.<folder name>]`: `tracked_branch` (what counts as main), `group`, `ignore` (hide it),
`keep_branches`. All of these can also be set from the repo menu.

## Groups

`gtnh` when `origin` is in the org, `other` otherwise. A repo in a subfolder of the root (for example
`IdeaProjects/_External/ForgeEssentials`) takes the subfolder's name as its group.

## IntelliJ

Found through `ide_command`, else a JetBrains Toolbox script (`idea`), else `idea` on PATH. When none works,
`o` asks for the path and saves it. Not using IntelliJ? Answer `off` there (or in Settings) and the IDE actions
disappear.

## Pack mods outside the org

Their source repo and branch are known for most of them, from the GitHub links in DreamAssemblerXXL or from
a list shipped with NH Workbench; add or override one under
`[pack.sources."<mod name>"]`.
