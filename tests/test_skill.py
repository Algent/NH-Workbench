import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from nh_workbench import skill
from nh_workbench.cli import app

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
    assert which[:3] == ["uv", "run", "--quiet"] and which[-2:] == [str(REPO), "nh-workbench"]


def test_launcher_without_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(skill, "own_checkout", lambda: None)
    folder, checkout = skill.install(tmp_path)
    assert checkout is None and not (folder / "checkout.txt").exists()

    res = launcher(folder, "--which", path="")  # nothing on PATH: the published version
    assert res.stdout.split()[:4] == ["uvx", "--quiet", "--from", skill_published(folder)]
    assert "no local NH Workbench checkout" in res.stderr

    bad = launcher(folder, "--set-checkout", str(tmp_path))
    assert bad.returncode == 2 and "not an NH Workbench checkout" in bad.stderr
    good = launcher(folder, "--set-checkout", str(REPO))
    assert good.returncode == 0 and (folder / "checkout.txt").read_text(encoding="utf-8") == str(REPO)

    skill.install(tmp_path)  # reinstalling from a non-checkout keeps the choice
    assert (folder / "checkout.txt").read_text(encoding="utf-8") == str(REPO)


def skill_published(folder: Path) -> str:
    text = (folder / "spark.py").read_text(encoding="utf-8")
    return text.split('PUBLISHED = "', 1)[1].split('"', 1)[0]


def test_install_for_codex_and_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(skill, "_home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    assert skill.detected() == ["claude"]  # nothing found: Claude Code
    (tmp_path / ".codex").mkdir()
    assert skill.detected() == ["codex"]
    (tmp_path / ".claude").mkdir()
    assert skill.detected() == ["claude", "codex"]

    runner = CliRunner()
    base = ["--root", str(tmp_path / "none"), "tools", "install-skill"]
    res = runner.invoke(app, [*base, "--for", "codex"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / ".agents" / "skills" / "spark-profile" / "SKILL.md").is_file()
    assert not skill.installed("claude") and skill.installed("codex")
    res = runner.invoke(app, base)  # both are found
    assert res.exit_code == 0 and skill.installed("claude") and "for Codex" in res.output
    assert (tmp_path / ".claude" / "commands" / "spark.md").is_file()  # /spark
    assert not (tmp_path / ".agents" / "commands").exists()  # Codex has no commands folder of its own
    assert runner.invoke(app, [*base, "--for", "gemini"]).exit_code == 2


def test_install_for_opencode(tmp_path, monkeypatch):
    monkeypatch.setattr(skill, "_home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    assert skill.detected() == ["opencode"]
    res = CliRunner().invoke(app, ["--root", str(tmp_path / "none"), "tools", "install-skill", "--for", "opencode"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / ".config" / "opencode" / "skills" / "spark-profile" / "SKILL.md").is_file()
    command = tmp_path / ".config" / "opencode" / "commands" / "spark.md"  # /spark: OpenCode may not pick the skill up
    text = command.read_text(encoding="utf-8")
    assert "spark-profile" in text and "$ARGUMENTS" in text
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "oc"))
    assert skill.skills_dir("opencode") == tmp_path / "oc" / "skills"


def test_launcher_accepts_a_checkout_from_before_the_rename(tmp_path):
    folder, _ = skill.install(tmp_path / "skills")
    old = tmp_path / "old"
    old.mkdir()
    (old / "pyproject.toml").write_text('[project]\nname = "gtnh-devenv-tools"\n', encoding="utf-8")
    assert launcher(folder, "--set-checkout", str(old)).returncode == 0
    assert launcher(folder, "--which").stdout.split()[-2:] == [str(old), "gtnh-dev"]


def test_refresh_updates_installed_skills_and_keeps_their_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(skill, "_home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    folder = tmp_path / ".claude" / "skills" / "spark-profile"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("old", encoding="utf-8")
    (folder / "checkout.txt").write_text("C:/elsewhere", encoding="utf-8")

    assert skill.refresh_installed() == ["claude"]
    assert "NH Workbench" in (folder / "SKILL.md").read_text(encoding="utf-8") and (folder / "spark.py").is_file()
    assert (folder / "checkout.txt").read_text(encoding="utf-8") == "C:/elsewhere"
    assert not (tmp_path / ".agents").exists()  # not installed for Codex: left alone


def test_a_spark_command_of_the_users_own_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(skill, "_home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    mine = tmp_path / ".claude" / "commands" / "spark.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("Light the campfire", encoding="utf-8")  # another /spark, not about this skill
    skill.install(agent="claude")
    assert mine.read_text(encoding="utf-8") == "Light the campfire"
