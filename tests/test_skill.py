import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from gtnh_devenv_tools import skill
from gtnh_devenv_tools.cli import app

REPO = Path(__file__).resolve().parents[1]


def launcher(folder: Path, *args: str, path: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": path} if path is not None else None
    script = folder / "spark.py"
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, env=env)


def test_install_points_the_skill_at_this_checkout(tmp_path):
    res = CliRunner().invoke(app, ["--root", str(tmp_path / "none"), "tools", "install-skill", "--dest", str(tmp_path)])
    assert res.exit_code == 0, res.output
    folder = tmp_path / "spark-profile"
    assert (folder / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: spark-profile\n")
    assert (folder / "checkout.txt").read_text(encoding="utf-8") == str(REPO)
    assert f"runs this checkout: {REPO}" in res.output.replace("\n", "")

    which = launcher(folder, "--which").stdout.split()
    assert which[:3] == ["uv", "run", "--quiet"] and which[-2:] == [str(REPO), "gtnh-dev"]


def test_launcher_without_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(skill, "own_checkout", lambda: None)
    folder, checkout = skill.install(tmp_path)
    assert checkout is None and not (folder / "checkout.txt").exists()

    res = launcher(folder, "--which", path="")  # nothing on PATH: the published version
    assert res.stdout.split()[:4] == ["uvx", "--quiet", "--from", skill_published(folder)]
    assert "no local gtnh-devenv-tools checkout" in res.stderr

    bad = launcher(folder, "--set-checkout", str(tmp_path))
    assert bad.returncode == 2 and "not a gtnh-devenv-tools checkout" in bad.stderr
    good = launcher(folder, "--set-checkout", str(REPO))
    assert good.returncode == 0 and (folder / "checkout.txt").read_text(encoding="utf-8") == str(REPO)

    skill.install(tmp_path)  # reinstalling from a non-checkout keeps the choice
    assert (folder / "checkout.txt").read_text(encoding="utf-8") == str(REPO)


def skill_published(folder: Path) -> str:
    text = (folder / "spark.py").read_text(encoding="utf-8")
    return text.split('PUBLISHED = "', 1)[1].split('"', 1)[0]
