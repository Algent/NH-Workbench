# gtnh-devenv-tools

Keeps track of a folder full of GTNH mod checkouts: what is off its main branch, what is behind,
which local branches still have an open PR, and which pack mods are checked out.

Needs `git`, and the `gh` CLI (logged in) for PR status. Reads `%USERPROFILE%\IdeaProjects` by default.

```
uv sync
uv run gtnh-dev              # terminal UI
uv tool install -e .         # optional: puts `gtnh-dev` on PATH
```

`gtnh-dev.cmd` starts the terminal UI from anywhere (double-click it, or point a shortcut at it).

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
`r` rescan · `o` open in IntelliJ · `g` open PR or GitHub page ·
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

The Reviews view lists open PRs that ask for your review personally. `t` there also loads the ones that
reach you through a team (slower), and `t` again hides them.

## Settings from the UI

These write to the config file and keep its comments:

- Extras (`x`) → change the repo folder.
- A repo's menu (`Enter`) → set its group, or hide it from the tool. To show a hidden repo again, remove
  its `ignore = true` line from the config file.

## Groups

Repos are grouped automatically: `gtnh` when `origin` is in the GTNewHorizons org, `other` otherwise.
A repo inside a subfolder of the root (for example `IdeaProjects\_External\ForgeEssentials`) takes the
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
Deleted branches are listed with the command to restore them (the UI logs them to
`%LOCALAPPDATA%\gtnh-devenv-tools\Cache\deleted-branches.log`).

## Development

```
uv run pytest
uv run ruff check
```
