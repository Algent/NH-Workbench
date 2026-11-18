# gtnh-devenv-tools

Keeps track of a folder full of GTNH mod checkouts: what is off its main branch, what is behind,
which local branches still have an open PR, and which pack mods are checked out.

Needs `git`, and the `gh` CLI (logged in) for PR status. Reads `IdeaProjects` in your home folder by default.
Works on Windows, Linux and macOS.

```
uv sync
uv run gtnh-dev              # terminal UI
uv tool install -e .         # optional: puts `gtnh-dev` on PATH
```

`gtnh-dev.cmd` (Windows) or `gtnh-dev.sh` (Linux, macOS) starts the terminal UI from anywhere: double-click
it, or point a shortcut at it.

## Where files go

| | Windows | Linux | macOS |
|---|---|---|---|
| Config | `%APPDATA%\gtnh-devenv-tools\config.toml` | `~/.config/gtnh-devenv-tools/config.toml` | `~/Library/Application Support/gtnh-devenv-tools/config.toml` |
| Cache, logs | `%LOCALAPPDATA%\gtnh-devenv-tools\Cache` | `~/.cache/gtnh-devenv-tools` | `~/Library/Caches/gtnh-devenv-tools` |

Linux follows `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` when set. Extras (`x`) shows the config path and opens
both folders.

IntelliJ is found through `ide_command` in the config, else a JetBrains Toolbox script (`idea`), else `idea`
on PATH. When none of these works, `o` asks for the path and saves it; Extras → "IntelliJ launcher" changes it.

## Commands

| Command | What it does |
|---|---|
| `gtnh-dev` | Terminal UI |
| `gtnh-dev status [--off-main] [--behind] [--dirty] [--group G] [--fetch] [--pack]` | One line per repo |
| `gtnh-dev fetch` | Fetch every repo, move nothing |
| `gtnh-dev pull [--dry-run]` | Fetch, then fast-forward main where safe |
| `gtnh-dev to-main` | Switch repos parked on a merged PR branch back to main, then fast-forward |
| `gtnh-dev branches [--cleanup]` | Local branches and their PR; optionally delete merged ones |
| `gtnh-dev reviews [--team]` | Open PRs waiting on your review |
| `gtnh-dev pack [--missing] [--refresh]` | Mods of the current daily and the latest release, and whether they are cloned |
| `gtnh-dev clone NAME...` | Clone pack mods into the root |

Global options: `--root PATH`, `-v`.

## What "pull" does

Only fast-forwards, never merges, rebases or forces.

- On main, no tracked changes, no local commits: fast-forward to its upstream.
- On main but dirty, ahead, diverged or mid-merge/rebase: skipped and flagged.
- On another branch: the local main is fast-forwarded in the background, your branch and files are untouched.
- Feature branches are never moved.

`--dry-run` reports what would move as of the last fetch and touches nothing.

## Terminal UI keys

`Enter` or a click on the highlighted row: actions for that one repo (fetch, pull, switch to main, delete merged branches, open) ·
`Tab` next view · `/` filter · `s` or a click on a column header: sort · `f` fetch all · `p` pull safe · `F` / `P` same for the selected repo ·
`r` rescan · `o` open in IntelliJ · `w` open PR or web page ·
`m` switch repos on a merged branch back to main · `d` delete merged branches ·
`c` clone (Pack view) · `x` extras (refresh pack data, open the config or logs folder) · `q` quit

## Pack data

Read from DreamAssemblerXXL's `master` on GitHub, at most once an hour (`pack --refresh` or `x` in the UI
forces it): the daily manifest, the manifest of the latest stable release, and the number of the last
successful daily build. The Pack view lists every mod of either, with its version in each.

Repos get a Daily and a Release column (`status --pack` on the command line), both as of your last fetch:

- `+N` after the daily version: main has N commits the daily's version lacks (merged, not in a daily yet).
- `+N` after the released version: the repo's `release/<major>.<minor>.x` branch has N commits since that
  version's tag. The UI's detail pane lists them.

The My PRs view lists your own open PRs across GitHub with review and CI state, and the local branch
each one comes from when it is checked out here.

The Reviews view lists open PRs that ask for your review personally. `t` there also loads the ones that
reach you through a team (slower), and `t` again hides them.

The All PRs view lists every open PR of the org (`org` in the config), marked when it is yours, asks
for your review, or you reviewed it. It searches GitHub only when the tab is opened and the last search
is over 30 minutes old (about 20 seconds); otherwise it shows the copy saved in the cache folder.
Auto-refresh leaves it alone. `r` there searches again, `t` shows or hides drafts.

The PR views show each PR's size (`+added -removed` lines). `s` there sorts by last update, smallest
first, largest first or repo (a click on the Size header too), and a filter like `<200` keeps PRs under
200 changed lines.

## Settings from the UI

These write to the config file and keep its comments:

- Extras (`x`) → change the repo folder, or the auto-refresh interval (default 15 minutes, 0 turns it
  off). Auto-refresh rescans like `r`; it does not fetch.
- A repo's menu (`Enter`) → set its main branch or its group, or hide it from the tool. Extras → "Show a
  hidden repo again" undoes the hiding.

## Groups

Repos are grouped automatically: `gtnh` when `origin` is in the GTNewHorizons org, `other` otherwise.
A repo inside a subfolder of the root (for example `IdeaProjects/_External/ForgeEssentials`) takes the
subfolder name as its group. `group`, `tracked_branch` and `ignore` can be set per repo in the config,
see `config.example.toml`.

## Closed-source mods

`gtnh-dev jars` (or Extras → "Download closed-source jars") fetches the jar of every pack mod without
public source into `_GTNH_Closed_Source` under the root, from where DreamAssemblerXXL gets it. Jars
already there are left alone.

## Forks and worktrees

- A checkout whose `origin` is a fork kept only to push branches (PRs go to another remote's repo) is
  matched to the pack through that other remote, and PRs are looked up in both repos.
- Git worktrees under the root are listed with a `↳` and the repo they belong to. A branch checked out
  in another working tree is never moved, switched to or deleted from elsewhere; the tool says where
  it lives instead.

## Branch cleanup

A branch is offered for deletion only when its PR is merged and every local commit is part of that PR.
Branches listed in a repo's `keep_branches` (repo menu → "Protect branches from cleanup") are never
offered, and show `(kept)` in branch lists. Extras → "Unprotect a kept branch" lifts it again.
Deleted branches are listed with the command to restore them (the UI logs them to
`deleted-branches.log` in the cache folder).

## Development

```
uv run pytest
uv run ruff check
```

GitHub Actions runs both on Linux and Windows for every push and pull request (`.github/workflows/ci.yml`).
