"""Install the spark-profile skill for Claude Code (user level, so it works in every project)."""

import os
from importlib import resources
from pathlib import Path

SKILL = "spark-profile"
FILES = ("SKILL.md", "spark.py")


def skills_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "skills"


def own_checkout() -> Path | None:
    """The checkout this copy of the tool runs from, when it is one (not an installed wheel)."""
    root = Path(__file__).resolve().parents[2]
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    return root if 'name = "gtnh-devenv-tools"' in text else None


def install(dest: Path | None = None) -> tuple[Path, Path | None]:
    """Copy the skill into dest (default: the user's Claude skills folder). Returns its folder and the checkout
    it was pointed at, if any; an earlier choice is kept when this copy is not a checkout."""
    folder = (dest or skills_dir()) / SKILL
    folder.mkdir(parents=True, exist_ok=True)
    source = resources.files(__package__).joinpath("claude_skill")
    for name in FILES:
        (folder / name).write_text(source.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    checkout = own_checkout()
    if checkout:
        (folder / "checkout.txt").write_text(str(checkout), encoding="utf-8")
    return folder, checkout
