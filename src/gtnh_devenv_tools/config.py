import os
import shutil
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import platformdirs
import tomlkit
import tomlkit.exceptions

from . import APP_NAME


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RepoConfig:
    tracked_branch: str | None = None
    ignore: bool = False
    group: str | None = None
    keep_branches: tuple[str, ...] = ()  # never deleted by the cleanup


def _repo_config(values: dict) -> RepoConfig:
    keep = values.get("keep_branches", ())
    return RepoConfig(**{**values, "keep_branches": tuple([keep] if isinstance(keep, str) else keep)})


@dataclass
class Config:
    root: Path
    org: str = "GTNewHorizons"
    concurrency: int = 8
    refresh_minutes: int = 15  # the UI rescans this often while it is open; 0 turns it off
    prune: bool = True
    clone_protocol: str = "ssh"  # ssh or https, for clones and remotes the tool adds
    refresh_fetch: bool = False  # auto-refresh fetches instead of only rescanning
    pr_filters: tuple[str, ...] = ()  # filters on the All PRs tab, see github.PR_FILTERS
    ide_command: str | None = None
    ide_off: bool = False  # ide_command = false: no IDE actions at all
    cleanup_to_main: bool = True  # cleanup offers branches still checked out, switching to main first
    repos: dict[str, RepoConfig] = field(default_factory=dict)
    # external pack mod name (lower case) -> {repo, branch, closed}
    sources: dict[str, dict] = field(default_factory=dict)

    def repo(self, name: str) -> RepoConfig:
        return self.repos.get(name.lower(), RepoConfig())

    @property
    def slug_branches(self) -> dict[str, str]:
        """Source repos whose relevant branch is not the default one."""
        return {
            f"{s.get('host', 'github.com')}/{s['repo']}".lower(): s["branch"]
            for s in self.sources.values()
            if s.get("repo") and s.get("branch")
        }

    @property
    def hidden(self) -> list[str]:
        return sorted(name for name, repo in self.repos.items() if repo.ignore)

    @property
    def kept(self) -> list[tuple[str, str]]:
        """(repo, branch) pairs protected from cleanup."""
        return sorted((name, br) for name, repo in self.repos.items() for br in repo.keep_branches)


def config_path() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True)) / "config.toml"


def cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))


def _shipped_sources() -> dict[str, dict]:
    text = resources.files(__package__).joinpath("pack_sources.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def load(root: Path | None = None, path: Path | None = None) -> Config:
    path = path or config_path()
    data: dict = {}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as e:
            raise ConfigError(f'Could not read config "{path}": {e}') from e

    sources = {k.lower(): v for k, v in _shipped_sources().items()}
    sources.update({k.lower(): v for k, v in data.get("pack", {}).get("sources", {}).items()})
    try:
        return Config(
            root=Path(root or data.get("root") or Path.home() / "IdeaProjects").expanduser(),
            org=str(data.get("org", "GTNewHorizons")),
            concurrency=max(1, int(data.get("concurrency", 8))),
            refresh_minutes=max(0, int(data.get("refresh_minutes", 15))),
            prune=bool(data.get("prune", True)),
            clone_protocol="https" if str(data.get("clone_protocol", "ssh")).lower() == "https" else "ssh",
            refresh_fetch=bool(data.get("refresh_fetch", False)),
            pr_filters=tuple(str(f) for f in data.get("pr_filters", ())),
            ide_command=ide if isinstance(ide := data.get("ide_command"), str) and ide else None,
            ide_off=ide is False,
            cleanup_to_main=bool(data.get("cleanup_to_main", True)),
            repos={k.lower(): _repo_config(v) for k, v in data.get("repos", {}).items()},
            sources=sources,
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f'Invalid config "{path}": {e}') from e


def save_setting(keys: tuple[str, ...], value: str | bool | int | list | None, path: Path | None = None) -> Path:
    """Set (or with None, remove) one value in the config file, keeping its comments and layout."""
    path = path or config_path()
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else tomlkit.document()
    except (tomlkit.exceptions.TOMLKitError, OSError) as e:
        raise ConfigError(f'Could not read config "{path}": {e}') from e
    table = doc
    for wanted in keys[:-1]:
        # Repo names are matched without regard to case, so reuse the spelling already in the file.
        key = next((k for k in table if k.lower() == wanted.lower()), wanted)
        if key not in table:
            table[key] = tomlkit.table()
        table = table[key]
    if value is None:
        table.pop(keys[-1], None)
    else:
        table[keys[-1]] = value
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    except OSError as e:
        raise ConfigError(f'Could not write config "{path}": {e}') from e
    return path


def runnable(command: str) -> bool:
    """A file that exists, or a command found on PATH."""
    return Path(command).expanduser().is_file() or shutil.which(command) is not None


def _toolbox_scripts() -> Path:
    """Where JetBrains Toolbox puts its launcher scripts (it calls them "shell scripts")."""
    return Path(platformdirs.user_data_dir("JetBrains", appauthor=False, roaming=False)) / "Toolbox" / "scripts"


# Folders people commonly keep their checkouts in, relative to the home folder.
_ROOT_GUESSES = (
    "IdeaProjects Projects projects dev Dev git Git src code repos workspace Documents/GitHub source/repos"
).split()


def suggest_root(home: Path | None = None) -> Path | None:
    """A likely checkout folder: the first common one holding a git repo, else the first that exists."""
    home = home or Path.home()
    existing = [home / name for name in _ROOT_GUESSES if (home / name).is_dir()]
    for folder in existing:
        try:
            if any((child / ".git").exists() for child in folder.iterdir()):
                return folder
        except OSError:
            continue
    return existing[0] if existing else None


def find_ide(cfg: Config, scripts: Path | None = None) -> str | None:
    """IntelliJ launcher: ide_command from the config, a Toolbox script, then whatever is on PATH."""
    if cfg.ide_off:
        return None
    if cfg.ide_command:
        return cfg.ide_command
    scripts = scripts or _toolbox_scripts()
    if scripts.is_dir():
        wanted = {".cmd"} if os.name == "nt" else {"", ".sh"}
        found = sorted(p for p in scripts.glob("idea*") if p.is_file() and p.suffix.lower() in wanted)
        if found:
            return str(found[0])
    names = ("idea64", "idea")
    if os.name != "nt":
        names = ("idea", "idea.sh", "intellij-idea-ultimate", "intellij-idea-community")  # last two: snap packages
    return next((found for name in names if (found := shutil.which(name))), None)
