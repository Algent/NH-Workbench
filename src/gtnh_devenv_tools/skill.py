"""Install the spark-profile skill for coding agents (user level, so it works in every project).

Claude Code and Codex read the same skill format: a folder with SKILL.md (name, description) and its files.
"""

import os
from importlib import resources
from pathlib import Path

SKILL = "spark-profile"
FILES = ("SKILL.md", "spark.py")
AGENTS = {"claude": "Claude Code", "codex": "Codex"}


def _home() -> Path:
    return Path.home()


def skills_dir(agent: str = "claude") -> Path:
    """Where an agent looks for the user's own skills."""
    if agent == "codex":
        return _home() / ".agents" / "skills"
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else _home() / ".claude") / "skills"


def detected() -> list[str]:
    """The agents that look installed for this user (their folder exists); Claude Code when none does."""
    found = []
    if os.environ.get("CLAUDE_CONFIG_DIR") or (_home() / ".claude").is_dir():
        found.append("claude")
    if (_home() / ".codex").is_dir() or (_home() / ".agents").is_dir():
        found.append("codex")
    return found or ["claude"]


def installed(agent: str) -> bool:
    return (skills_dir(agent) / SKILL / "SKILL.md").is_file()


def own_checkout() -> Path | None:
    """The checkout this copy of the tool runs from, when it is one (not an installed wheel)."""
    root = Path(__file__).resolve().parents[2]
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    return root if 'name = "gtnh-devenv-tools"' in text else None


def install(dest: Path | None = None, agent: str = "claude") -> tuple[Path, Path | None]:
    """Copy the skill into dest (default: the agent's skills folder). Returns its folder and the checkout it was
    pointed at, if any; an earlier choice is kept when this copy is not a checkout."""
    folder = (dest or skills_dir(agent)) / SKILL
    folder.mkdir(parents=True, exist_ok=True)
    source = resources.files(__package__).joinpath("agent_skill")
    for name in FILES:
        (folder / name).write_text(source.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    checkout = own_checkout()
    if checkout:
        (folder / "checkout.txt").write_text(str(checkout), encoding="utf-8")
    return folder, checkout
