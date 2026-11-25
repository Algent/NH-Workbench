# Pack and GitHub data

## Pack data

Read from DreamAssemblerXXL's `master` on GitHub, at most once an hour (`pack --refresh` or Settings forces
it): the daily manifest, the latest stable release, and the number of the last successful daily build.

The Daily and Release columns (`status --pack` on the command line) count commits as of your last fetch:

- `1.9.0 +3` in Daily: main has 3 commits the daily's version lacks (merged, not in a daily yet).
- `1.8.18 +2` in Release: the `release/<major>.<minor>.x` branch has 2 commits since that version's tag.
- `Δ17` in Release: main has 17 commits since that tag; the higher, the harder a backport.

Closed-source mods: `nh-workbench jars` (or the Tools tab) downloads their jars into `_GTNH_Closed_Source`
under the root, where DreamAssemblerXXL expects them. Jars already there are left alone.

Pack mods outside the org: see [Configuration](Configuration#pack-mods-outside-the-org).

## GitHub data

PR information comes from GitHub through the [`gh` CLI](https://cli.github.com/); without it logged in,
everything but the PR information still works. The repo tabs and My PRs reload on every rescan, Reviews too
(team requests only on demand, they are slow to search).

All PRs searches GitHub only when the tab is opened and its last search is over 30 minutes old; it takes
about 20 seconds, and otherwise shows the copy saved in the cache folder. Auto-refresh leaves it alone.
Its filters (`v`) and `nh-workbench prs --org` work on that saved copy, so changing them searches nothing.
