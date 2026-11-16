# gtnh-devenv-tools

Keeps track of a folder full of GTNH mod checkouts: what is off its main branch, what is behind,
which local branches still have an open PR, and which pack mods are checked out.

Needs `git`, and the `gh` CLI (logged in) for PR status. Reads `%USERPROFILE%\IdeaProjects` by default.

```
uv sync
uv run gtnh-dev              # terminal UI
uv tool install -e .         # optional: puts `gtnh-dev` on PATH
```

## Commands

| Command | What it does |
|---|---|
| `gtnh-dev` | Terminal UI |
| `gtnh-dev status [--off-main] [--behind] [--dirty] [--group G] [--fetch]` | One line per repo |
| `gtnh-dev fetch` | Fetch every repo, move nothing |
| `gtnh-dev pull [--dry-run]` | Fetch, then fast-forward main where safe |
| `gtnh-dev to-main` | Switch repos parked on a merged PR branch back to main, then fast-forward |
| `gtnh-dev branches [--cleanup]` | Local branches and their PR; optionally delete merged ones |
| `gtnh-dev pack [--missing] [--refresh]` | Mods of the current daily and whether they are cloned |
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
`Tab` next view · `/` filter · `f` fetch all · `p` pull safe · `F` / `P` same for the selected repo ·
`r` rescan · `o` open in IntelliJ · `g` open PR or GitHub page ·
`m` switch repos on a merged branch back to main · `d` delete merged branches ·
`c` clone (Pack view) · `C` open the config folder · `L` open the logs/cache folder · `q` quit

## Groups

Repos are grouped automatically: `gtnh` when `origin` is in the GTNewHorizons org, `other` otherwise.
A repo inside a subfolder of the root (for example `IdeaProjects\_External\ForgeEssentials`) takes the
subfolder name as its group. `group`, `tracked_branch` and `ignore` can be set per repo in the config,
see `config.example.toml`.

## Branch cleanup

A branch is offered for deletion only when its PR is merged and every local commit is part of that PR.
Deleted branches are listed with the command to restore them (the UI logs them to
`%LOCALAPPDATA%\gtnh-devenv-tools\Cache\deleted-branches.log`).

## Development

```
uv run pytest
uv run ruff check
```
