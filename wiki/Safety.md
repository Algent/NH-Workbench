# What the tool will and won't do

Nothing is ever pushed.

## Pull

Pull only fast-forwards: it never merges, rebases or forces.

- On main, clean, no local commits: fast-forward to its upstream.
- On main but dirty, ahead, diverged or mid-merge/rebase: skipped and flagged.
- On another branch: the local main is fast-forwarded in the background; your branch and files are untouched.
- Feature branches are never moved. `--dry-run` shows what would move and touches nothing.

## Branch cleanup

Deletes a branch only when its PR is merged and every local commit is part of that PR. Branches in a repo's
`keep_branches` are never offered, and show `(kept)`.

In the UI, a merged branch that is still checked out is offered too when the repo is clean: a checkbox (`s`)
switches those repos to main first, or leaves their branch alone; the choice is remembered.

Each deletion is listed with the command that restores it, and logged to `deleted-branches.log` in the cache
folder. A fork remote that no branch uses any more is removed too.

## PR checkout

`c` on a PR tab refuses a repo with uncommitted changes. A PR from the org repo checks out its own branch name.
A PR from a fork becomes `PR-<number>`, tracking the fork's branch through a remote named after its owner
(added when missing). It then shows behind or gone like any other branch, and the cleanup deletes it, and that
remote, once merged. Pressing `c` again switches back to the branch and fast-forwards it.

## Worktrees

Worktrees under the root are listed with a `↳` and the repo they belong to. A branch checked out in another
working tree is never moved, switched to or deleted from elsewhere.

## Forks

A checkout whose `origin` is a fork kept only to push branches is matched to the pack through its other
remote, and PRs are looked up in both repos.

## Built jars

Only `*.jar` files directly in `build/libs` of checkouts with a remote in the org are deleted, for good (not to
the recycle bin); the next build makes them again.
