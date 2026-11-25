# Command line

`nhwb` is a short name for `nh-workbench`; the old `gtnh-dev` still works too.

| Command | What it does |
|---|---|
| `nh-workbench status [--off-main] [--behind] [--dirty] [--group G] [--fetch] [--pack]` | One line per repo |
| `nh-workbench fetch` | Fetch every repo, move nothing |
| `nh-workbench pull [--dry-run]` | Fetch, then fast-forward main where safe |
| `nh-workbench to-main` | Switch repos parked on a merged PR branch back to main, then fast-forward |
| `nh-workbench branches [--cleanup]` | Local branches and their PR; optionally delete merged ones |
| `nh-workbench reviews [--team]` | Open PRs waiting on your review |
| `nh-workbench prs [--mine \| --org] [--sort small\|large\|repo] [--max-size N] [--local] [--pack] [--unreviewed] [--stale]` | Review requests, your PRs or the org's, sorted and filtered like the UI (`--help` for more) |
| `nh-workbench pack [--missing] [--refresh]` | Pack mods and whether they are checked out |
| `nh-workbench clone NAME...` | Clone pack mods into the root |
| `nh-workbench jars` | Download the jars of closed-source pack mods |
| `nh-workbench tools spark ...` | Read a spark profile, see [spark profiles](Spark-Profiles) |
| `nh-workbench tools install-skill [--for claude\|codex\|opencode\|all]` | Install the spark-profile skill for coding agents |

Global options: `--root PATH`, `-v` (log every command run). `--help` after any command lists its options.

What pull and the branch cleanup touch, and what they leave alone: [What the tool will and won't do](Safety).
