import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

from . import APP_NAME, git
from .config import Config, cache_dir
from .models import PackEntry, RepoState
from .proc import Result

log = logging.getLogger(__name__)

_BASE = "https://raw.githubusercontent.com/GTNewHorizons/DreamAssemblerXXL/master/"
_GITHUB_URL = re.compile(r"github\.com/([^/]+)/([^/#?]+?)(?:\.git)?(?:/tree/([^?#]+?))?/?$", re.IGNORECASE)
_SLIM_KEYS = ("name", "license", "source", "external_url", "repo_url", "private")
_MAX_AGE = 24 * 3600


class PackError(Exception):
    pass


def _slim_assets(data: dict) -> list[dict]:
    # The asset index is ~16 MB because of per-version changelogs; keep only what is used here.
    return [{k: mod.get(k) for k in _SLIM_KEYS} for mod in data["mods"]]


def _cached_json(url: str, dest: Path, transform: Callable | None = None, force: bool = False):
    etag_file = dest.with_name(dest.name + ".etag")
    if force or not dest.exists() or time.time() - dest.stat().st_mtime > _MAX_AGE:
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        if dest.exists() and etag_file.exists():
            req.add_header("If-None-Match", etag_file.read_text(encoding="utf-8").strip())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
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


def build_entries(manifest: dict, assets: list[dict], sources: dict[str, dict]) -> list[PackEntry]:
    by_name = {a["name"].lower(): a for a in assets}
    entries = []
    for name, info in manifest.get("github_mods", {}).items():
        asset = by_name.get(name.lower(), {})
        repo, _ = parse_github_url(asset.get("repo_url"))
        entries.append(
            PackEntry(
                name=name,
                kind="github",
                version=info.get("version", ""),
                repo=repo or f"GTNewHorizons/{name}",
                private=bool(asset.get("private")),
                license=asset.get("license"),
                url=asset.get("repo_url"),
            )
        )
    for name, info in manifest.get("external_mods", {}).items():
        asset = by_name.get(name.lower(), {})
        repo, branch = parse_github_url(asset.get("external_url"))
        source = sources.get(name.lower(), {})
        entries.append(
            PackEntry(
                name=name,
                kind="external",
                version=info.get("version", ""),
                repo=source.get("repo", repo),
                branch=source.get("branch", branch),
                closed=bool(source.get("closed")),
                license=asset.get("license"),
                url=asset.get("external_url"),
            )
        )
    return sorted(entries, key=lambda e: e.name.lower())


def link_local(entries: Iterable[PackEntry], states: Iterable[RepoState]) -> None:
    """Match pack mods to local checkouts by repository, and label the checkouts."""
    by_slug: dict[str, RepoState] = {}
    for st in states:
        st.pack = ""
        for slug in (st.origin.slug if st.origin else None, st.canonical):
            if slug:
                by_slug[slug.lower()] = st
    for entry in entries:
        entry.local = by_slug.get((entry.repo or "").lower())
        if entry.local:
            entry.local.pack = f"{'daily' if entry.kind == 'github' else 'ext'} {entry.version}"


def load_sync(cfg: Config, force: bool = False) -> list[PackEntry]:
    cache = cache_dir()
    manifest = _cached_json(_BASE + "releases/manifests/daily.json", cache / "daily.json", force=force)
    assets = _cached_json(_BASE + "gtnh-assets.json", cache / "assets-slim.json", _slim_assets, force=force)
    return build_entries(manifest, assets, cfg.sources)


async def load(cfg: Config, force: bool = False) -> list[PackEntry]:
    return await asyncio.to_thread(load_sync, cfg, force)


def find(entries: Iterable[PackEntry], name: str) -> PackEntry | None:
    wanted = name.lower()
    return next((e for e in entries if wanted in (e.name.lower(), (e.repo or "").lower().partition("/")[2])), None)


async def clone(entry: PackEntry, root: Path) -> Result:
    if not entry.repo:
        return Result(1, "", "closed source" if entry.closed else "source repository unknown")
    dest = root / entry.repo.partition("/")[2]
    if dest.exists():
        return Result(1, "", f"{dest} already exists")
    return await git.clone(f"https://github.com/{entry.repo}.git", dest, entry.branch)
