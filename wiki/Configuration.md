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
| `theme` | Textual's | The UI's theme; picking one in the command palette (`Ctrl+P`) saves it here |
| `refresh_fetch` | `false` | Auto-refresh fetches every repo instead of only rescanning |
| `pr_filters` | none | All PRs filters, set with `v`: `local`, `pack`, `unreviewed`, `stale` |
| `concurrency` | `8` | Repos handled at the same time |
| `prune` | `true` | Drop stale remote-tracking refs on fetch |
| `ide_command` | found automatically | IntelliJ launcher; `false` hides the IDE actions |
| `cleanup_to_main` | `true` | Cleanup's "switch to main first" checkbox starts ticked |

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
