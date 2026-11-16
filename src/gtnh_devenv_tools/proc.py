"""The only module that spawns processes."""

import asyncio
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Never block on a credential prompt, never take optional index locks (keeps out of the IDE's way).
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_OPTIONAL_LOCKS": "0"}
_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def message(self) -> str:
        lines = (self.err or self.out).strip().splitlines()
        return lines[-1].strip() if lines else f"exit code {self.code}"


async def run(*args: str, cwd: Path | None = None, stdin: str | None = None, timeout: float = 120) -> Result:
    log.debug("run %s (cwd=%s)", " ".join(args), cwd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=_ENV,
            creationflags=_FLAGS,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return Result(127, "", str(e))
    try:
        data = stdin.encode() if stdin is not None else None
        out, err = await asyncio.wait_for(proc.communicate(data), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return Result(124, "", f"timed out after {timeout:.0f}s")
    return Result(proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def open_path(path: Path) -> None:
    """Show a folder in the system file manager."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        launch("open" if sys.platform == "darwin" else "xdg-open", str(path))


def launch(*args: str) -> None:
    """Start a detached GUI program (IDE) without waiting for it."""
    cmd = list(args)
    if sys.platform == "win32" and Path(args[0]).suffix.lower() in {".cmd", ".bat"}:
        cmd = ["cmd", "/c", *args]
    log.debug("launch %s", cmd)
    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_FLAGS
    )
