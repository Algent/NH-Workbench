# Terminal UI

Started by `nh-workbench` (or `nhwb`) without a command, or by the `nh-workbench.cmd` / `nh-workbench.sh`
launchers.

![The terminal UI on the All tab](images/terminal-ui.svg)

## Tabs

| Tab | Shows |
|---|---|
| All, Off main, Behind, Dirty, Branches, Other | Repos, one line each, filtered |
| My PRs | Your open PRs across GitHub, with review and CI state and the local branch they come from |
| Reviews | Open PRs asking for your review personally (`t` adds the ones that reach you through a team) |
| All PRs | Every open PR of the org, marked when you wrote it, are asked to review it, or reviewed it |
| Pack | Every mod of the current daily and the latest release, and whether it is checked out |
| Tools | spark profiles, clone missing repos, closed-source jars, clean built jars, install the AI skill |

The panel at the bottom gives details on the selected line: branches and their PRs, the last commit,
commits waiting on the release branch.

## Keys

| Key | Action |
|---|---|
| `Enter`, click | Repo tabs: menu for that repo. PR tabs: open the PR |
| `Tab` / `Shift+Tab` | Next / previous tab |
| `/` | Filter by name. On PR tabs, `<200` keeps PRs under 200 changed lines |
| `s`, click on a header | Sort. PR tabs: last update, smallest, largest, repo |
| `f` / `p` | Fetch all / pull all where safe (`F` / `P`: selected repo only) |
| `r` | Rescan (on My PRs and Reviews: refresh them from GitHub; on All PRs: search GitHub again) |
| `m` | Switch repos parked on a merged PR branch back to main |
| `d` | Delete merged branches |
| `o` / `w` | Open in IntelliJ / open the PR or GitHub page |
| `t` | Reviews: show team requests. All PRs: show drafts |
| `v` | All PRs: show only PRs in repos checked out here, in pack mods, without any review, or not updated for 30+ days |
| `c` | Pack tab: clone the selected mod. PR tabs: check out the PR in its local repo |
| `x` | Settings |
| `q` | Quit (also closes menus) |

## Repo menu

`Enter` on a repo: fetch, pull, switch to main, delete merged branches, open in IntelliJ, on GitHub or in the
file manager, set the main branch or group, protect branches from cleanup, hide the repo.

## Settings

`x`: auto-refresh interval, fetch during auto-refresh (off by default), IntelliJ launcher, clone over SSH or
HTTPS, refresh pack data, change the repo folder, show a hidden repo again, unprotect a kept branch, open the
config or cache folder, and update the tool itself (its checkout is fetched at start; a notification says when
there are new commits, and after updating, restart it).

Settings changed from the UI are written to the config file, keeping its comments. See
[Configuration](Configuration) for everything the file holds.
