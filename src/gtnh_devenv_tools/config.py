import os
import shutil
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import platformdirs

from . import APP_NAME


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RepoConfig:
    tracked_branch: str | None = None
    ignore: bool = False
    group: str | None = None


@dataclass
class Config:
    root: Path
    org: str = "GTNewHorizons"
    concurrency: int = 8
    prune: bool = True
    ide_command: str | None = None
    repos: dict[str, RepoConfig] = field(default_factory=dict)
    # external pack mod name (lower case) -> {repo, branch, closed}
    sources: dict[str, dict] = field(default_factory=dict)

    def repo(self, name: str) -> RepoConfig:
        return self.repos.get(name.lower(), RepoConfig())

    @property
    def slug_branches(self) -> dict[str, str]:
        """Source repos whose relevant branch is not the default one."""
        return {s["repo"].lower(): s["branch"] for s in self.sources.values() if s.get("repo") and s.get("branch")}


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
            prune=bool(data.get("prune", True)),
            ide_command=data.get("ide_command"),
            repos={k.lower(): RepoConfig(**v) for k, v in data.get("repos", {}).items()},
            sources=sources,
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f'Invalid config "{path}": {e}') from e


def find_ide(cfg: Config) -> str | None:
    if cfg.ide_command:
        return cfg.ide_command
    scripts = Path(os.environ.get("LOCALAPPDATA", "")) / "JetBrains" / "Toolbox" / "scripts"
    if scripts.is_dir():
        found = sorted(scripts.glob("idea*.cmd"))
        if found:
            return str(found[0])
    return shutil.which("idea64") or shutil.which("idea")
