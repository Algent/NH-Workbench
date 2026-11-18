# gtnh-devenv-tools

Keeps a folder full of GTNH mod checkouts under control:

- which repos are off their main branch, behind, or dirty, at a glance
- fetch everything, and fast-forward main where it is safe, even while you are on a feature branch
- local branches with their PR, and cleanup of the merged ones
- your PRs, your review requests, and every open PR of the org, sortable by size
- where each repo stands in the pack: daily and release versions, what still needs a backport
- clone missing pack mods, download the closed-source jars

Works on Windows, Linux and macOS.

## Getting started

Needs [uv](https://docs.astral.sh/uv/), `git`, and the [`gh` CLI](https://cli.github.com/) logged in
(without it, everything but the PR information still works).

```
uv sync
uv run gtnh-dev              # terminal UI
uv tool install -e .         # optional: puts `gtnh-dev` on PATH
```

`gtnh-dev.cmd` (Windows) and `gtnh-dev.sh` (Linux, macOS) start the terminal UI from anywhere: run them
directly or point a shortcut at them. Command-line arguments are passed through.

Checkouts are read from `IdeaProjects` in your home folder; change it with `--root`, `root` in the config,
or Extras in the UI.

## Terminal UI

### Tabs

| Tab | Shows |
|---|---|
| All, Off main, Behind, Dirty, Branches, Other | Repos, one line each, filtered |
| My PRs | Your open PRs across GitHub, with review and CI state and the local branch they come from |
| Reviews | Open PRs asking for your review personally (`t` adds the ones that reach you through a team) |
| All PRs | Every open PR of the org, marked when you wrote it, are asked to review it, or reviewed it |
| Pack | Every mod of the current daily and the latest release, and whether it is checked out |

The panel at the bottom gives details on the selected line: branches and their PRs, the last commit,
commits waiting on the release branch.

### Keys

| Key | Action |
|---|---|
| `Enter`, click | Repo tabs: menu for that repo. PR tabs: open the PR |
| `Tab` / `Shift+Tab` | Next / previous tab |
| `/` | Filter by name. On PR tabs, `<200` keeps PRs under 200 changed lines |
| `s`, click on a header | Sort. PR tabs: last update, smallest, largest, repo |
| `f` / `p` | Fetch all / pull all where safe (`F` / `P`: selected repo only) |
| `r` | Rescan (on All PRs: search GitHub again) |
| `m` | Switch repos parked on a merged PR branch back to main |
| `d` | Delete merged branches |
| `o` / `w` | Open in IntelliJ / open the PR or GitHub page |
| `t` | Reviews: show team requests. All PRs: show drafts |
| `c` | Clone the selected mod (Pack tab) |
| `x` | Extras |
| `q` | Quit (also closes menus) |

**Repo menu** (`Enter`): fetch, pull, switch to main, delete merged branches, open in IntelliJ, on GitHub or
in the file manager, set the main branch or group, protect branches from cleanup, hide the repo.

**Extras** (`x`): auto-refresh interval, IntelliJ launcher, refresh pack data, change the repo folder,
show a hidden repo again, unprotect a kept branch, open the config or cache folder, download closed-source
jars.

Settings changed from the UI are written to the config file, keeping its comments.

## Command line

| Command | What it does |
|---|---|
| `gtnh-dev status [--off-main] [--behind] [--dirty] [--group G] [--fetch] [--pack]` | One line per repo |
| `gtnh-dev fetch` | Fetch every repo, move nothing |
| `gtnh-dev pull [--dry-run]` | Fetch, then fast-forward main where safe |
| `gtnh-dev to-main` | Switch repos parked on a merged PR branch back to main, then fast-forward |
| `gtnh-dev branches [--cleanup]` | Local branches and their PR; optionally delete merged ones |
| `gtnh-dev reviews [--team]` | Open PRs waiting on your review |
| `gtnh-dev pack [--missing] [--refresh]` | Pack mods and whether they are checked out |
| `gtnh-dev clone NAME...` | Clone pack mods into the root |
| `gtnh-dev jars` | Download the jars of closed-source pack mods |

Global options: `--root PATH`, `-v` (log every command run).

## What the tool will and won't do

**Pull** only fast-forwards: it never merges, rebases or forces.

- On main, clean, no local commits: fast-forward to its upstream.
- On main but dirty, ahead, diverged or mid-merge/rebase: skipped and flagged.
- On another branch: the local main is fast-forwarded in the background; your branch and files are untouched.
- Feature branches are never moved. `--dry-run` shows what would move and touches nothing.

**Branch cleanup** deletes a branch only when its PR is merged and every local commit is part of that PR.
Branches in a repo's `keep_branches` are never offered, and show `(kept)`. Each deletion is listed with the
command that restores it, and logged to `deleted-branches.log` in the cache folder. A fork remote that no
branch uses any more is removed too.

**Worktrees** under the root are listed with a `↳` and the repo they belong to. A branch checked out in
another working tree is never moved, switched to or deleted from elsewhere.

**Forks**: a checkout whose `origin` is a fork kept only to push branches is matched to the pack through its
other remote, and PRs are looked up in both repos.

Nothing is ever pushed.

## Pack data

Read from DreamAssemblerXXL's `master` on GitHub, at most once an hour (`pack --refresh` or Extras forces
it): the daily manifest, the latest stable release, and the number of the last successful daily build.

The Daily and Release columns (`status --pack` on the command line) count commits as of your last fetch:

- `1.9.0 +3` in Daily: main has 3 commits the daily's version lacks (merged, not in a daily yet).
- `1.8.18 +2` in Release: the `release/<major>.<minor>.x` branch has 2 commits since that version's tag.
- `Δ17` in Release: main has 17 commits since that tag; the higher, the harder a backport.

Closed-source mods: `gtnh-dev jars` (or Extras) downloads their jars into `_GTNH_Closed_Source` under the
root, where DreamAssemblerXXL expects them. Jars already there are left alone.

## GitHub data

PR information comes from GitHub through `gh`. The repo tabs and My PRs reload on every rescan, Reviews
too (team requests only on demand, they are slow to search).

All PRs searches GitHub only when the tab is opened and its last search is over 30 minutes old; it takes
about 20 seconds, and otherwise shows the copy saved in the cache folder. Auto-refresh leaves it alone.

## Configuration

Everything is optional; see `config.example.toml`.

| | Windows | Linux | macOS |
|---|---|---|---|
| Config | `%APPDATA%\gtnh-devenv-tools\config.toml` | `~/.config/gtnh-devenv-tools/config.toml` | `~/Library/Application Support/gtnh-devenv-tools/config.toml` |
| Cache, logs | `%LOCALAPPDATA%\gtnh-devenv-tools\Cache` | `~/.cache/gtnh-devenv-tools` | `~/Library/Caches/gtnh-devenv-tools` |

Linux follows `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` when set.

| Setting | Default | |
|---|---|---|
| `root` | `~/IdeaProjects` | Folder holding the checkouts |
| `org` | `GTNewHorizons` | Its repos form the `gtnh` group; All PRs lists its PRs |
| `refresh_minutes` | `15` | The UI rescans this often (no fetch); `0` turns it off |
| `concurrency` | `8` | Repos handled at the same time |
| `prune` | `true` | Drop stale remote-tracking refs on fetch |
| `ide_command` | found automatically | IntelliJ launcher |

Per repo, under `[repos.<folder name>]`: `tracked_branch` (what counts as main), `group`, `ignore` (hide it),
`keep_branches`. All of these can also be set from the repo menu.

**Groups**: `gtnh` when `origin` is in the org, `other` otherwise. A repo in a subfolder of the root (for
example `IdeaProjects/_External/ForgeEssentials`) takes the subfolder's name as its group.

**IntelliJ** is found through `ide_command`, else a JetBrains Toolbox script (`idea`), else `idea` on PATH.
When none works, `o` asks for the path and saves it.

**Pack mods outside the org**: their source repo and branch are known for most of them; add or override
one under `[pack.sources."<mod name>"]`.

## Development

```
uv run pytest
uv run ruff check
```

GitHub Actions runs both on Linux and Windows for every push and pull request.
