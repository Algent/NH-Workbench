import os
import subprocess
from pathlib import Path

import pytest

from gtnh_devenv_tools import proc
from gtnh_devenv_tools.config import Config


class Sandbox:
    """A bare origin, a seed clone that plays 'someone else pushing', and the work clone under test."""

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.env = env
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.root = root / "projects"
        self.work = self.root / "repo"
        self.root.mkdir()
        self.git(root, "init", "--bare", "-b", "master", str(self.origin))
        self.git(root, "clone", str(self.origin), str(self.seed))
        self.commit(self.seed, "first")
        self.git(self.seed, "push", "-u", "origin", "master")
        self.git(root, "clone", str(self.origin), str(self.work))
        self.cfg = Config(root=self.root)

    def git(self, cwd: Path, *args: str) -> str:
        res = subprocess.run(["git", "-C", str(cwd), *args], env=self.env, check=True, capture_output=True, text=True)
        return res.stdout.strip()

    def commit(self, cwd: Path, name: str) -> str:
        (cwd / f"{name}.txt").write_text(name, encoding="utf-8")
        self.git(cwd, "add", "-A")
        self.git(cwd, "commit", "-m", name)
        return self.sha(cwd)

    def sha(self, cwd: Path, ref: str = "HEAD") -> str:
        return self.git(cwd, "rev-parse", ref)

    def push_upstream(self, name: str) -> str:
        sha = self.commit(self.seed, name)
        self.git(self.seed, "push", "origin", "master")
        return sha


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    isolated = {
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    for key, value in isolated.items():
        monkeypatch.setitem(proc._ENV, key, value)
    return Sandbox(tmp_path, {**os.environ, **isolated})


@pytest.fixture(autouse=True)
def no_mcp_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never fetch the MCP mappings in tests: spark profiles keep their func_ names."""
    from gtnh_devenv_tools import spark

    monkeypatch.setattr(spark, "mcp_names", lambda minecraft: {})
