import asyncio
import gzip
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import APP_NAME, git
from .config import Config, cache_dir
from .models import CloneJob, PackEntry, PackInfo, RepoState
from .proc import Result

log = logging.getLogger(__name__)

_BASE = "https://raw.githubusercontent.com/GTNewHorizons/DreamAssemblerXXL/master/"
_GITHUB_URL = re.compile(r"github\.com/([^/]+)/([^/#?]+?)(?:\.git)?(?:/tree/([^?#]+?))?/?$", re.IGNORECASE)
_STABLE = re.compile(r"^\d+(\.\d+)+$")
_SLIM_KEYS = ("name", "license", "source", "external_url", "repo_url", "private")
_MAX_AGE = 3600


class PackError(Exception):
    pass


JAR_FOLDER = "_GTNH_Closed_Source"
_JAR_KEYS = ("version_tag", "filename", "download_url")


def _slim_mod(mod: dict) -> dict:
    slim = {k: mod.get(k) for k in _SLIM_KEYS}
    if mod.get("source"):  # hosted outside GitHub: remember where each version's jar comes from
        slim["versions"] = [{k: v.get(k) for k in _JAR_KEYS} for v in mod.get("versions") or []]
    return slim


def _slim_assets(data: dict) -> dict:
    # The asset index is ~16 MB because of per-version changelogs; keep only what is used here.
    return {
        "mods": [_slim_mod(mod) for mod in data["mods"]],
        "latest_daily": data.get("latest_daily"),
        "latest_successful_daily": data.get("latest_successful_daily"),
    }


def _cached_json(url: str, dest: Path, transform: Callable | None = None, force: bool = False):
    etag_file = dest.with_name(dest.name + ".etag")
    if force or not dest.exists() or time.time() - dest.stat().st_mtime > _MAX_AGE:
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME, "Accept-Encoding": "gzip"})
        if dest.exists() and etag_file.exists():
            req.add_header("If-None-Match", etag_file.read_text(encoding="utf-8").strip())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                data = json.loads(body)
                etag = resp.headers.get("ETag")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_text(json.dumps(transform(data) if transform else data), encoding="utf-8")
            tmp.replace(dest)
            if etag:
                etag_file.write_text(etag, encoding="utf-8")
        except urllib.error.HTTPError as e:
            if e.code != 304 or not dest.exists():
                raise PackError(f"Could not download {url}: {e}") from e
            os.utime(dest)
        except (OSError, ValueError, KeyError) as e:
            if not dest.exists():
                raise PackError(f"Could not download {url}: {e}") from e
            log.warning("Using cached %s, refresh failed: %s", dest.name, e)
    return json.loads(dest.read_text(encoding="utf-8"))


def parse_github_url(url: str | None) -> tuple[str | None, str | None]:
    """Return (owner/name, branch) for a GitHub URL, (None, None) for anything else."""
    m = _GITHUB_URL.search(url or "")
    return (f"{m[1]}/{m[2]}", m[3]) if m else (None, None)


def latest_release(names: Iterable[str]) -> str | None:
    """Highest plain version among DAXXL's release names (betas, RCs and dailies are skipped)."""
    stable = [n for n in names if _STABLE.match(n)]
    return max(stable, key=lambda n: tuple(int(part) for part in n.split(".")), default=None)


def release_branch(release: str) -> str:
    """Maintenance branch of a release in the mod repos: 2.9.0 -> release/2.9.x."""
    return "release/" + ".".join(release.split(".")[:2]) + ".x"


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _new_entry(name: str, kind: str, asset: dict, source: dict) -> PackEntry:
    if kind == "github":
        repo, _ = parse_github_url(asset.get("repo_url"))
        return PackEntry(
            name=name,
            kind=kind,
            version="",
            repo=repo or f"GTNewHorizons/{name}",
            private=bool(asset.get("private")),
            license=asset.get("license"),
            url=asset.get("repo_url"),
        )
    repo, branch = parse_github_url(asset.get("external_url"))
    if not repo:  # downloaded from elsewhere (CurseForge, a forum), but DAXXL may still know the source
        repo, branch = parse_github_url(asset.get("repo_url"))
    return PackEntry(
        name=name,
        kind=kind,
        version="",
        repo=source.get("repo", repo),
        host=source.get("host", "github.com"),
        branch=source.get("branch", branch),
        closed=bool(source.get("closed")),
        dev_status=source.get("status", ""),
        note=source.get("note", ""),
        license=asset.get("license"),
        url=asset.get("external_url"),
    )


def build_entries(daily: dict, release: dict, assets: list[dict], sources: dict[str, dict]) -> list[PackEntry]:
    """One entry per mod found in the daily build, the latest release, or both."""
    by_name = {a["name"].lower(): a for a in assets}
    entries: dict[str, PackEntry] = {}
    for manifest, field_name in ((daily, "version"), (release, "release_version")):
        for kind in ("github", "external"):
            for name, info in manifest.get(f"{kind}_mods", {}).items():
                key = name.lower()
                if key not in entries:
                    entries[key] = _new_entry(name, kind, by_name.get(key, {}), sources.get(key, {}))
                setattr(entries[key], field_name, info.get("version", ""))
    for key, entry in entries.items():
        wanted = entry.version or entry.release_version
        versions = by_name.get(key, {}).get("versions") or []
        jar = next((v for v in versions if v.get("version_tag") == wanted and v.get("download_url")), None)
        if jar:
            entry.jar_url = jar["download_url"]
            entry.jar_name = jar.get("filename") or jar["download_url"].rpartition("/")[2]
    return sorted(entries.values(), key=lambda e: e.name.lower())


@dataclass
class JarReport:
    downloaded: list[str] = field(default_factory=list)
    present: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [f"{len(self.downloaded)} downloaded", f"{self.present} already there"]
        return ", ".join(parts + ([f"{len(self.failed)} failed"] if self.failed else []))


def _download_jar(entry: PackEntry, folder: Path) -> str:
    """Returns "present", "downloaded" or an error text. Never overwrites an existing jar."""
    dest = folder / Path(entry.jar_name).name
    if dest.exists():
        return "present"
    tmp = dest.with_name(dest.name + ".part")
    try:
        req = urllib.request.Request(entry.jar_url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=120) as resp:
            tmp.write_bytes(resp.read())
        tmp.replace(dest)
    except (OSError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        return str(e)
    return "downloaded"


async def download_closed(entries: Iterable[PackEntry], folder: Path) -> JarReport:
    """Fetch the jars of mods without public source, from where DreamAssemblerXXL gets them."""
    report = JarReport()
    folder.mkdir(parents=True, exist_ok=True)
    closed = [e for e in entries if e.closed]
    results = await asyncio.gather(
        *(asyncio.to_thread(_download_jar, e, folder) if e.jar_url else _no_jar() for e in closed)
    )
    for entry, result in zip(closed, results, strict=True):
        if result == "present":
            report.present += 1
        elif result == "downloaded":
            report.downloaded.append(entry.jar_name)
        else:
            report.failed.append(f"{entry.name}: {result}")
    return report


async def _no_jar() -> str:
    return "no download known for this version"


def link_local(entries: Iterable[PackEntry], states: Iterable[RepoState]) -> None:
    """Match pack mods to local checkouts by repository, and copy their versions onto the checkouts."""
    states = list(states)
    by_key: dict[str, RepoState] = {}
    for st in states:
        st.daily = st.release = ""
        renamed = f"github.com/{st.canonical}".lower() if st.canonical else None
        for key in (git.remote_key(st.origin.url) if st.origin else None, renamed):
            if key:
                by_key[key] = st
    # Then the other remotes, for forks kept only to push branches towards an upstream. Origins win.
    for st in states:
        for remote in st.remotes.values():
            key = git.remote_key(remote.url)
            if key:
                by_key.setdefault(key, st)
    for entry in entries:
        entry.local = by_key.get(entry.key or "")
        if entry.local:
            entry.local.daily, entry.local.release = entry.version, entry.release_version


async def load(cfg: Config, force: bool = False) -> PackInfo:
    cache = cache_dir()
    for pattern in ("assets-slim.json*", "assets.json*"):  # cache layouts of earlier versions
        for stale in cache.glob(pattern):
            stale.unlink(missing_ok=True)

    def get(remote: str, local: str, transform: Callable | None = None):
        return asyncio.to_thread(_cached_json, _BASE + remote, cache / local, transform, force)

    daily, assets, modpack = await asyncio.gather(
        get("releases/manifests/daily.json", "daily.json"),
        get("gtnh-assets.json", "assets-v2.json", _slim_assets),
        get("gtnh-modpack.json", "modpack.json"),
    )
    name = latest_release(modpack.get("releases", []))
    release: dict = {}
    if name:
        try:
            release = await get(f"releases/manifests/{name}.json", f"release-{name}.json")
        except PackError as e:
            log.warning("No manifest for release %s: %s", name, e)
    return PackInfo(
        entries=build_entries(daily, release, assets["mods"], cfg.sources),
        release=name or "",
        release_updated=_when(release.get("last_updated")),
        daily_config=daily.get("config", ""),
        daily_updated=_when(daily.get("last_updated")),
        daily_build=assets.get("latest_successful_daily"),
        daily_attempt=assets.get("latest_daily"),
        fetched=datetime.fromtimestamp((cache / "daily.json").stat().st_mtime, UTC),
    )


def find(entries: Iterable[PackEntry], name: str) -> PackEntry | None:
    wanted = name.lower()
    return next((e for e in entries if wanted in (e.name.lower(), (e.repo or "").lower().partition("/")[2])), None)


def clone_job(entry: PackEntry, root: Path, protocol: str = "ssh") -> CloneJob | None:
    if not entry.repo:
        return None
    url = git.clone_url(entry.host, entry.repo, protocol)
    return CloneJob(entry.repo, url, root / entry.repo.partition("/")[2], entry.branch)


def missing_jobs(entries: Iterable[PackEntry], root: Path, protocol: str = "ssh") -> list[CloneJob]:
    """Pack mods with a known, public source that are not checked out and whose folder is free."""
    jobs = (clone_job(e, root, protocol) for e in entries if e.status == "missing" and not e.private)
    by_dest = {j.dest: j for j in jobs if j and not j.dest.exists()}  # one repo can ship several mods
    return list(by_dest.values())


async def clone(entry: PackEntry, root: Path, protocol: str = "ssh") -> Result:
    job = clone_job(entry, root, protocol)
    if not job:
        return Result(1, "", "closed source" if entry.closed else "source repository unknown")
    if job.dest.exists():
        return Result(1, "", f"{job.dest} already exists")
    res = await git.clone(job.url, job.dest, job.branch)
    return res if res.ok else Result(res.code, "", git.clone_error(res))


def pack_slugs(entries: Iterable[PackEntry]) -> set[str]:
    """Lower-case owner/name of the pack's source repos, with the current name of renamed ones checked out here."""
    slugs = {e.repo.lower() for e in entries if e.repo}
    slugs |= {e.local.slug.lower() for e in entries if e.local and e.local.slug}
    return slugs
