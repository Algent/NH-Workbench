# /// script
# requires-python = ">=3.12"
# ///
"""Run `nh-workbench tools spark ...` wherever NH Workbench lives. Used by the spark-profile Claude Code skill.

Order: the checkout saved next to this file (--set-checkout PATH), else nh-workbench (or its old name gtnh-dev) on
PATH, else the published version from GitHub through uvx. Standard library only, so `uv run --no-project spark.py`
needs nothing else.
"""

import shutil
import subprocess
import sys
from pathlib import Path

SAVED = Path(__file__).resolve().parent / "checkout.txt"
PUBLISHED = "git+https://github.com/Algent/NH-Workbench"
# Project name in pyproject.toml -> its command. gtnh-devenv-tools is the old name, for checkouts not updated yet.
COMMANDS = {"nh-workbench": "nh-workbench", "gtnh-devenv-tools": "gtnh-dev"}


def checkout_command(path: Path) -> str | None:
    """The command of the NH Workbench checkout at path, None when it is not one."""
    try:
        text = (path / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    return next((cmd for name, cmd in COMMANDS.items() if f'name = "{name}"' in text), None)


def command() -> list[str]:
    if SAVED.is_file():
        checkout = Path(SAVED.read_text(encoding="utf-8").strip())
        if cmd := checkout_command(checkout):
            return ["uv", "run", "--quiet", "--project", str(checkout), cmd]
        print(f"note: the saved checkout {checkout} is gone; set another with --set-checkout PATH", file=sys.stderr)
    for cmd in COMMANDS.values():
        if shutil.which(cmd):
            return [cmd]
    print(
        "note: no local NH Workbench checkout set, using the published version from GitHub"
        " (use a checkout with --set-checkout PATH)",
        file=sys.stderr,
    )
    return ["uvx", "--quiet", "--from", PUBLISHED, "nh-workbench"]


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--set-checkout"]:
        if len(args) != 2:
            print("usage: spark.py --set-checkout PATH", file=sys.stderr)
            return 2
        path = Path(args[1]).expanduser().resolve()
        if not checkout_command(path):
            print(f"{path} is not an NH Workbench checkout (no matching pyproject.toml)", file=sys.stderr)
            return 2
        SAVED.write_text(str(path), encoding="utf-8")
        print(f"saved: {path}")
        return 0
    if args[:1] == ["--which"]:
        print(" ".join(command()))
        return 0
    try:
        return subprocess.call([*command(), "tools", "spark", *args])
    except FileNotFoundError as e:
        print(f"cannot run {e.filename}: install uv (https://docs.astral.sh/uv/)", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
