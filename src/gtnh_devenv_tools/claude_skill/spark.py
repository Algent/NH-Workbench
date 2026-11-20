# /// script
# requires-python = ">=3.12"
# ///
"""Run `gtnh-dev tools spark ...` wherever gtnh-devenv-tools lives. Used by the spark-profile Claude Code skill.

Order: the checkout saved next to this file (--set-checkout PATH), else gtnh-dev on PATH, else the published
version from GitHub through uvx. Standard library only, so `uv run --no-project spark.py` needs nothing else.
"""

import shutil
import subprocess
import sys
from pathlib import Path

SAVED = Path(__file__).resolve().parent / "checkout.txt"
PUBLISHED = "git+https://github.com/Algent/gtnh-devenv-tools"


def is_checkout(path: Path) -> bool:
    try:
        return 'name = "gtnh-devenv-tools"' in (path / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False


def command() -> list[str]:
    if SAVED.is_file():
        checkout = Path(SAVED.read_text(encoding="utf-8").strip())
        if is_checkout(checkout):
            return ["uv", "run", "--quiet", "--project", str(checkout), "gtnh-dev"]
        print(f"note: the saved checkout {checkout} is gone; set another with --set-checkout PATH", file=sys.stderr)
    if shutil.which("gtnh-dev"):
        return ["gtnh-dev"]
    print(
        "note: no local gtnh-devenv-tools checkout set, using the published version from GitHub"
        " (use a checkout with --set-checkout PATH)",
        file=sys.stderr,
    )
    return ["uvx", "--quiet", "--from", PUBLISHED, "gtnh-dev"]


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--set-checkout"]:
        if len(args) != 2:
            print("usage: spark.py --set-checkout PATH", file=sys.stderr)
            return 2
        path = Path(args[1]).expanduser().resolve()
        if not is_checkout(path):
            print(f"{path} is not a gtnh-devenv-tools checkout (no matching pyproject.toml)", file=sys.stderr)
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
