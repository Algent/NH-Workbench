"""What both UIs keep besides their widgets: repo states, PR and pack data, and the batches that update them.

Runs on an asyncio loop: Textual's own in the terminal UI, a background thread's in the desktop UI. The callbacks are
called from that loop, so the desktop UI brings them over to the Qt thread.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import github, ops, pack, render, skill, views
from .config import Config
from .models import Branch, CloneJob, PackEntry, PackInfo, RepoState, ReviewItem

FAILURES_SHOWN = 5  # failures told one by one before the summary
# notify's severity, as Textual names it: "information", "warning" or "error".
Notify = Callable[[str, str], None]


class Workbench:
    def __init__(
        self,
        cfg: Config,
        on_repo: Callable[[RepoState], None],
        on_change: Callable[[list[RepoState], list[PackEntry], dict[str, list[ReviewItem]]], None],
        on_progress: Callable[[str, int, int], None],
        notify: Notify,
        name_of: Callable[[str], str] | None = None,
    ) -> None:
        """on_repo: one repo was scanned. on_change: the lists changed (repos, pack mods, PR tab -> its PRs).
        on_progress: label, done, total (done is 0 as a batch starts); an empty label when the job is over.
        name_of: how a message names an action of keys.ACTIONS, with its key, as in "Settings (x)"."""
        self.cfg = cfg
        self.on_repo, self.on_change, self.on_progress, self.notify = on_repo, on_change, on_progress, notify
        self.name_of = name_of or (lambda action: action)
        self.states: dict[str, RepoState] = {}
        self.prs = github.PrData()
        self.pack_info: PackInfo | None = None
        self.mine: list[ReviewItem] = []
        self.reviews: list[ReviewItem] = []
        self.team_reviews = False  # also the requests that reach you through a team (slow to search)
        self.org: github.OrgPrs | None = None  # every open PR of the org, searched when All PRs opens
        self.org_searching = False
        self.busy = ""  # the batch running now; one at a time
        self.depth = 0  # steps of the job in progress: a cleanup scans, a clone too
        self.waiting: set[str] = set()  # repos (paths) the batch has yet to start on
        self.working: set[str] = set()  # repos it is on now
        self.unlinked = False  # pack data came in during a batch: link it to every repo when the batch ends
        self.tool: RepoState | None = None  # the tool's own checkout, once fetched at start
        self.tool_updated = False

    @property
    def entries(self) -> list[PackEntry]:
        return self.pack_info.entries if self.pack_info else []

    @contextlib.asynccontextmanager
    async def job(self):
        """A step of what a UI started. The UI hears that the job is over once, when its outermost step ends:
        a cleanup's switch and scan must not hand the toolbar back halfway."""
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            if not self.depth:
                self.on_progress("", 0, 0)

    def _changed(self) -> None:
        org = list(self.org.items) if self.org else []
        prs = {"mine": list(self.mine), "reviews": list(self.reviews), "org": org}  # PR tab -> its PRs
        self.on_change(sorted(self.states.values(), key=ops.by_repo), list(self.entries), prs)

    # --- loading -------------------------------------------------------------

    async def load(self, fetch: bool = False) -> None:
        """Scan (or fetch) every repo and load the PRs and the pack data.

        The pack data, your PRs and review requests do not need the repos: they load during the scan. The PRs of
        local branches do, and wait for it."""
        worker = ops.fetch_repo if fetch else ops.scan_repo
        # Independent sources: none of them should hold the others up.
        early = asyncio.gather(self._load_pack(), self._load_mine(), self._load_reviews(quiet=True))
        try:
            scanned = await self.batch("Fetching" if fetch else "Scanning", lambda p: worker(self.cfg, p), reset=True)
        except BaseException:
            early.cancel()
            raise
        await (asyncio.gather(early, self._load_prs()) if scanned else early)

    async def batch(
        self,
        label: str,
        worker: Callable[[Path], Awaitable[RepoState]],
        paths: list[Path] | None = None,
        reset: bool = False,
    ) -> bool:
        """Run worker on each repo (all of them when paths is None). False when another batch is running.

        reset: drop the repos that are gone from the root."""
        async with self.job():
            if self.busy:
                self.notify(f"{self.busy} is still running", "warning")
                return False
            self.busy = label
            try:
                if paths is None:
                    paths = ops.discover(self.cfg)
                if reset:
                    keep = {str(p) for p in paths}
                    self.states = {k: v for k, v in self.states.items() if k in keep}
                    self._changed()
                done = 0

                def on_done(st: RepoState) -> None:
                    nonlocal done
                    done += 1
                    github.attach([st], self.prs)
                    old = self.states.get(str(st.path))
                    if old:  # keep the pack columns steady until they are recomputed after the batch
                        st.daily, st.release, st.release_ahead = old.daily, old.release, old.release_ahead
                        st.daily_ahead, st.release_commits = old.daily_ahead, old.release_commits
                        st.release_delta = old.release_delta
                    self.states[str(st.path)] = st
                    self.on_repo(st)
                    self.on_progress(label, done, len(paths))

                async def tracked(path: Path) -> RepoState:
                    key = str(path)
                    self.waiting.discard(key)
                    self.working.add(key)
                    try:
                        return await worker(path)
                    finally:
                        self.working.discard(key)

                self.waiting = {str(p) for p in paths}
                self.on_progress(label, 0, len(paths))
                await ops.run_all(paths, tracked, self.cfg.concurrency, on_done)
            finally:
                self.busy = ""
                self.waiting.clear()
                self.working.clear()
            await self._relink(None if self.unlinked else paths)
            self.unlinked = False
            self._changed()
            return True

    async def _load_prs(self) -> None:
        data = await github.fetch_prs(self.states.values())
        if data.error:
            self.notify(f"PR status unavailable: {data.error}", "warning")
        if data.prs or data.canonical or not data.error:
            self.prs = data
            github.attach(self.states.values(), data)
            await self._relink()  # canonical names can change which pack mod a repo matches
            self._changed()

    async def _load_mine(self) -> None:
        items, error = await github.fetch_mine()
        if items or not error:
            self.mine = items
            self._changed()

    async def _load_reviews(self, quiet: bool = False) -> None:
        items, error = await github.fetch_reviews(self.team_reviews)
        if error and not quiet:
            self.notify(f"Review requests: {error}", "warning")
        if items or not error:
            self.reviews = items
            self._changed()

    async def reload_prs(self) -> None:
        """Load your PRs, review requests and branch PRs again, without rescanning the repos."""
        async with self.job():
            await asyncio.gather(self._load_prs(), self._load_mine(), self._load_reviews())

    async def toggle_team(self, on: bool) -> None:
        """Show or hide the review requests that reach you through a team."""
        async with self.job():
            self.team_reviews = on
            await self._load_reviews()

    async def open_org(self) -> None:
        """Show the saved org search at once; search again only when it is missing or old."""
        if self.org_searching:
            return
        if self.org is None:
            self.org = github.cached_org(self.cfg.org)
            if self.org:
                self._changed()
        if self.org is None or time.time() - self.org.fetched > github.ORG_MAX_AGE:
            await self.reload_org()

    async def reload_org(self) -> None:
        if self.org_searching:
            self.notify(f"Still searching the PRs of {self.cfg.org}", "information")
            return
        self.org_searching = True
        self.notify(f"Searching all open PRs of {self.cfg.org}, this takes a while...", "information")
        try:
            found = await github.fetch_org(self.cfg.org)
        finally:
            self.org_searching = False
        if found.error:
            self.notify(f"Org PRs: {found.error}", "warning")
        if found.items or not found.error:
            self.org = found
            self._changed()
            if not found.error:
                self.notify(f"Searched all open PRs of {self.cfg.org}", "information")

    async def _load_pack(self, force: bool = False) -> None:
        try:
            self.pack_info = await pack.load(self.cfg, force)
        except pack.PackError as e:
            self.notify(str(e), "warning")
        if self.busy:  # half the repos are not scanned yet: the batch links them all when it ends
            self.unlinked = True
        else:
            await self._relink()
        self._changed()

    async def refresh_pack(self) -> None:
        """Download the pack data again instead of using the copy cached for an hour."""
        async with self.job():
            await self._load_pack(force=True)

    async def _relink(self, paths: list[Path] | None = None) -> None:
        """Copy pack versions onto the repos, and recount release-branch commits for the given ones."""
        if not self.pack_info:
            return
        pack.link_local(self.pack_info.entries, self.states.values())
        keys = self.states if paths is None else [str(p) for p in paths]
        states = [self.states[k] for k in keys if k in self.states]
        branch = pack.release_branch(self.pack_info.release) if self.pack_info.release else ""
        await ops.pack_progress(states, branch, self.cfg.concurrency)

    # --- actions -------------------------------------------------------------

    async def checkout(self, item: ReviewItem, st: RepoState) -> None:
        """Check out a pull request in its local repo."""
        async with self.job():
            head, error = await github.pr_head(item.repo, item.pr.number, self.cfg.clone_protocol)
            if not head:
                self.notify(f"#{item.pr.number}: {error}", "error")
                return
            if not await self.batch("Checking out", lambda p: ops.checkout_pr(self.cfg, st, head), [st.path]):
                return
            new = self.states.get(str(st.path))
            if new and new.note:
                failed = new.note.kind == "failed"
                if not failed and new.head:
                    self.prs.prs[(new.name, new.head)] = item.pr  # link the PR now, not at the next PR reload
                    github.attach([new], self.prs)
                    self._changed()
                self.notify(f"{st.name}: {new.note.text}", "error" if failed else "information")

    async def find_cleanup(self, states: list[RepoState]) -> tuple[list[tuple[RepoState, Branch]], list]:
        """Merged branches safe to delete, and the merged ones kept for their local commits."""
        async with self.job():
            return await ops.cleanup_candidates(states, parked=True)

    async def cleanup(self, safe: list[tuple[RepoState, Branch]], parked: dict[Path, RepoState], switch: bool) -> None:
        """Delete merged branches; with switch, the parked repos go back to main first so theirs can go too."""
        async with self.job():
            if parked and switch:
                await self.batch("Switching", lambda p: ops.to_main(self.cfg, parked[p]), list(parked))
            # Whatever is still checked out (unticked, or the switch failed) stays.
            safe = [(st, br) for st, br in safe if self.states.get(str(st.path), st).head != br.name]
            if not safe:
                self.notify("Nothing deleted: the merged branches are still checked out", "information")
                return
            report = await ops.cleanup(safe)
            note, failed = views.finish_cleanup(report)
            await self.batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), list({st.path for st, _ in safe}))
            self.notify(note, "warning" if failed else "information")

    async def to_main_many(self, candidates: dict[Path, RepoState]) -> None:
        if await self.batch("Switching", lambda p: ops.to_main(self.cfg, candidates[p]), list(candidates)):
            self.notify(f"Switched. {self.name_of('cleanup')} deletes the merged branches.", "information")

    async def clone(self, entry: PackEntry) -> None:
        """Clone a pack mod into the root, then scan it so it shows up with the other checkouts."""
        async with self.job():
            res = await pack.clone(entry, self.cfg.root, self.cfg.clone_protocol)
            if not res.ok:
                self.notify(f"{entry.name}: {res.message}", "error")
                return
            job = pack.clone_job(entry, self.cfg.root, self.cfg.clone_protocol)
            await self.batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), [job.dest])
            self.notify(f"Cloned {entry.repo} into {job.dest}", "information")

    async def org_jobs(self) -> list[CloneJob] | None:
        """Clone jobs for the org repos not checked out here, archived ones left out. None when GitHub failed."""
        org = self.cfg.org
        self.notify(f"Listing the repos of {org}...", "information")
        repos, error = await github.org_repos(org)
        if error:
            self.notify(f"{org} repos: {error}", "error")
            if not repos:
                return None
        repos = [r for r in repos if not r.archived]
        return ops.org_jobs(repos, self.states.values(), self.cfg.root, self.cfg.clone_protocol)

    async def clone_many(self, jobs: list[CloneJob]) -> None:
        """Clone a few at a time, then scan what was cloned so it shows up with the other checkouts."""
        async with self.job():
            if self.busy:
                self.notify(f"{self.busy} is still running", "warning")
                return
            self.busy = "Cloning"
            done = 0

            def on_done(job: CloneJob, why: str | None) -> None:
                nonlocal done
                done += 1
                self.on_progress("Cloning", done, len(jobs))

            try:
                self.on_progress("Cloning", 0, len(jobs))
                failed = await ops.clone_all(jobs, min(4, self.cfg.concurrency), on_done)
            finally:
                self.busy = ""
            for job, why in failed[:FAILURES_SHOWN]:
                self.notify(f"{job.name}: {why}", "error")
            failed_jobs = {job for job, _ in failed}
            cloned = [j.dest for j in jobs if j.dest.is_dir() and j not in failed_jobs]
            if cloned:
                await self.batch("Scanning", lambda p: ops.scan_repo(self.cfg, p), cloned)
            more = f", {len(failed)} failed" if failed else ""
            self.notify(f"Cloned {len(cloned)} repos{more}", "warning" if failed else "information")

    async def download_jars(self) -> None:
        """The jars of closed-source pack mods, into the folder DreamAssemblerXXL expects."""
        async with self.job():
            if not any(e.closed for e in self.entries):
                self.notify("Pack data not loaded yet", "warning")
                return
            folder = self.cfg.root / pack.JAR_FOLDER
            self.notify("Downloading closed-source jars...", "information")
            report = await pack.download_closed(self.entries, folder)
            for line in report.failed:
                self.notify(line, "error")
            self.notify(f"{report.summary} in {folder}", "warning" if report.failed else "information")

    async def delete_jars(self, files: list[Path]) -> None:
        async with self.job():
            freed, failed = await asyncio.to_thread(ops.delete_files, files)
            for line in failed[:FAILURES_SHOWN]:
                self.notify(line, "error")
            more = f", {len(failed)} could not be deleted (in use?)" if failed else ""
            self.notify(f"Freed {render.megabytes(freed)}{more}", "warning" if failed else "information")

    async def install_skill(self, agents: list[str]) -> None:
        async with self.job():
            done = []
            for agent in agents:
                try:
                    folder, _ = await asyncio.to_thread(skill.install, None, agent)
                except OSError as e:
                    self.notify(f"{skill.AGENTS[agent]}: {e}", "error")
                    continue
                done.append(f"{skill.AGENTS[agent]} in {folder}")
            if done:
                self.notify(f"Skill installed for {', '.join(done)}; new sessions pick it up", "information")

    # --- the tool's own checkout ---------------------------------------------

    async def check_tool(self) -> None:
        """Fetch the tool's own checkout once per start, and say when it has new commits."""
        path = views.tool_checkout()
        if path is None:
            return
        self.tool = await ops.fetch_repo(self.cfg, path)
        behind = views.tool_behind(self.tool)
        if behind:
            self.notify(f"NH Workbench has {behind} new commit(s): {self.name_of('extras')} updates it", "information")

    async def update_tool(self) -> None:
        async with self.job():
            self.tool, message, updated = await views.update_tool(self.cfg, self.tool)
            self.tool_updated = self.tool_updated or updated
            if message:
                self.notify(message, "warning")
