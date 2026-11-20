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
| Tools | Tools that do not need the checkouts: for now, reading spark profiles |

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
| `v` | All PRs: show only PRs in repos checked out here, in pack mods, without any review, or not updated for 30+ days |
| `c` | Pack tab: clone the selected mod. PR tabs: check out the PR in its local repo |
| `x` | Extras |
| `q` | Quit (also closes menus) |

**Repo menu** (`Enter`): fetch, pull, switch to main, delete merged branches, open in IntelliJ, on GitHub or
in the file manager, set the main branch or group, protect branches from cleanup, hide the repo.

**Extras** (`x`): auto-refresh interval, fetch during auto-refresh (off by default), clone over SSH or HTTPS,
clone every missing pack mod or org repo (archived ones left out) after listing them, IntelliJ launcher, refresh pack data, change the repo folder,
show a hidden repo again, unprotect a kept branch, open the config or cache folder, download closed-source
jars, delete the jars Gradle left in `build/libs` of the org's repos (with the list and total size first).

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
| `gtnh-dev prs [--mine \| --org] [--sort small\|large\|repo] [--max-size N] [--local] [--pack] [--unreviewed] [--stale]` | Review requests, your PRs or the org's, sorted and filtered like the UI (`--help` for more) |
| `gtnh-dev pack [--missing] [--refresh]` | Pack mods and whether they are checked out |
| `gtnh-dev clone NAME...` | Clone pack mods into the root |
| `gtnh-dev jars` | Download the jars of closed-source pack mods |

Global options: `--root PATH`, `-v` (log every command run).

## Tools

Things that do not need the checkouts.

### spark profiles

Turns a [spark](https://spark.lucko.me) profile, the link players paste when reporting lag, into a short text
report that reads well for people and LLMs alike (a few thousand tokens, no screenshots):

```
gtnh-dev tools spark summary https://spark.lucko.me/XXXXXXXXXX
gtnh-dev tools spark tree XXXXXXXXXX makeCrater        # what a method calls, all its call sites merged
gtnh-dev tools spark callers XXXXXXXXXX ASMEventHandler.invoke   # who calls it
```

The summary has the platform, how the profile was recorded (every tick, or only ticks over N ms, which
changes how to read the percentages), TPS, MSPT, memory, GC, CPU and JVM arguments, the worst minutes, then
for the server thread: the methods with the most own time and total time, own time per mod, and the hot call
paths, with `[Mod]` where a mod's code starts.

- Method names: Minecraft's `func_` names are replaced with their MCP names (stable 12, what 1.7.10 development
  uses), from mappings downloaded once from Forge's Maven into the cache folder. Offline, the names stay as is.
- Own time per mod: time in native code, the JVM and libraries (netty, fastutil, guava) counts for the nearest
  mod up the call stack, else Minecraft or Forge. A backup's disk writes count for the backup mod, not for
  "native code".

In the UI: Tools tab, Enter on spark, then paste a link (saved profiles are listed). On the report, Enter on
a method shows what it calls, `t` / `c` what the method on the cursor line calls / who calls it (asked for when
the line has none), Enter on one of the worst minutes narrows to it, `w` / `h` pick a minute / thread, Esc steps back to the previous view (on the summary: closes), `q` closes, `s` jumps
straight to the summary, `y` copies the report (for an LLM), `e` saves it as a file, `o` opens another profile.

On the command line, `--thread NAME` picks another thread, `--window ID` (repeatable) narrows everything to the minutes listed under
"Worst minutes", `--min` and `--lines` control how much of the call tree is shown. A link or code also accepts a
downloaded `.sparkprofile` file. Downloads are kept in the cache folder under `spark`; `--refresh` downloads
again. The name and UUID of whoever recorded the profile are left out of the reports.

1.7.10 profiles do not say which mod a class comes from, so the tool ships a table of package prefixes per mod,
`src/gtnh_devenv_tools/mod_packages.toml`, generated from a pack's jars (classes of mixins count for the mod that
wrote them; anything the table misses is shown as `<package> (package)`). After mods were added or renamed,
regenerate it from the mods folders of recent packs, newest last:

```
uv run python scripts/gen_mod_packages.py "<2.9.0 client>/.minecraft/mods" "<2.9.0 server>/mods" "<daily>/.minecraft/mods"
```

It prints the packages several mods share without a clear owner; decide those in its `OVERRIDES`.

#### Skill for Claude Code and Codex

```
uv run gtnh-dev tools install-skill            # for the agents found in your home folder; --for claude|codex|all
```

(or F2 where the spark screen asks for a link) installs a `spark-profile` skill, so a spark link pasted in any project gets read
with these commands instead of the web viewer: for Claude Code in `~/.claude/skills`, for Codex in
`~/.agents/skills`. It runs through a small launcher that needs only uv: it uses the checkout it was installed
from, else `gtnh-dev` on PATH, else the published version from GitHub through `uvx` (the agent then offers once
to point it at a checkout).

## What the tool will and won't do

**Pull** only fast-forwards: it never merges, rebases or forces.

- On main, clean, no local commits: fast-forward to its upstream.
- On main but dirty, ahead, diverged or mid-merge/rebase: skipped and flagged.
- On another branch: the local main is fast-forwarded in the background; your branch and files are untouched.
- Feature branches are never moved. `--dry-run` shows what would move and touches nothing.

**Branch cleanup** deletes a branch only when its PR is merged and every local commit is part of that PR.
Branches in a repo's `keep_branches` are never offered, and show `(kept)`. In the UI, a merged branch that
is still checked out is offered too when the repo is clean: a checkbox (`s`) switches those repos to main
first, or leaves their branch alone; the choice is remembered. Each deletion is listed with the
command that restores it, and logged to `deleted-branches.log` in the cache folder. A fork remote that no
branch uses any more is removed too.

**PR checkout** (`c` on a PR tab) refuses a repo with uncommitted changes. A PR from the org repo checks out
its own branch name. A PR from a fork becomes `PR-<number>`, tracking the fork's branch through a remote named
after its owner (added when missing). It then shows behind or gone like any other branch, and the cleanup
deletes it, and that remote, once merged. Pressing `c` again switches back to the branch and fast-forwards it.

**Worktrees** under the root are listed with a `↳` and the repo they belong to. A branch checked out in
another working tree is never moved, switched to or deleted from elsewhere.

**Forks**: a checkout whose `origin` is a fork kept only to push branches is matched to the pack through its
other remote, and PRs are looked up in both repos.

**Built jars**: only `*.jar` files directly in `build/libs` of checkouts with a remote in the org are deleted,
for good (not to the recycle bin); the next build makes them again.

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
Its filters (`v`) and `gtnh-dev prs --org` work on that saved copy, so changing them searches nothing.

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
| `refresh_minutes` | `15` | The UI rescans this often; `0` turns it off |
| `clone_protocol` | `ssh` | `ssh` or `https`, for clones and the fork remotes PR checkout adds. Existing remotes are left alone |
| `refresh_fetch` | `false` | Auto-refresh fetches every repo instead of only rescanning |
| `pr_filters` | none | All PRs filters, set with `v`: `local`, `pack`, `unreviewed`, `stale` |
| `concurrency` | `8` | Repos handled at the same time |
| `prune` | `true` | Drop stale remote-tracking refs on fetch |
| `ide_command` | found automatically | IntelliJ launcher; `false` hides the IDE actions |
| `cleanup_to_main` | `true` | Cleanup's "switch to main first" checkbox starts ticked |

Per repo, under `[repos.<folder name>]`: `tracked_branch` (what counts as main), `group`, `ignore` (hide it),
`keep_branches`. All of these can also be set from the repo menu.

**Groups**: `gtnh` when `origin` is in the org, `other` otherwise. A repo in a subfolder of the root (for
example `IdeaProjects/_External/ForgeEssentials`) takes the subfolder's name as its group.

**IntelliJ** is found through `ide_command`, else a JetBrains Toolbox script (`idea`), else `idea` on PATH.
When none works, `o` asks for the path and saves it. Not using IntelliJ? Answer `off` there (or in Extras)
and the IDE actions disappear.

**Pack mods outside the org**: their source repo and branch are known for most of them; add or override
one under `[pack.sources."<mod name>"]`.

## Development

```
uv run pytest
uv run ruff check
uv run ruff format --check
```

GitHub Actions runs all three on Linux, Windows and macOS for every push and pull request.
