"""The only module that spawns processes."""

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
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
            # No controlling terminal: ssh fails at once instead of prompting over the UI for a host key.
            start_new_session=sys.platform != "win32",
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
        await _kill_tree(proc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 5)
        return Result(124, "", f"timed out after {timeout:.0f}s")
    except asyncio.CancelledError:
        # The caller gave up (a UI closing mid-fetch): stop the process instead of leaving it running, and wait
        # for it so its pipes close now rather than after the event loop is gone. Not for ever, should something
        # still hold the pipes: the cancelled task would never end and the loop never stop.
        await _kill_tree(proc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), 2)
        raise
    return Result(proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and what it started. On Windows, git.exe is a launcher that runs the real git as its child:
    killing the launcher alone leaves that one running, holding the pipes open."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid), creationflags=_FLAGS,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            await killer.wait()
    else:  # started in a session of its own: its process group holds what it started
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, 9)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()  # taskkill missing, or the group already gone


def open_path(path: Path) -> None:
    """Show a folder in the system file manager."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        launch("open" if sys.platform == "darwin" else "xdg-open", str(path))


# Tried in order on Linux after $TERMINAL; each starts in the folder it is started from.
_LINUX_TERMINALS = (
    "x-terminal-emulator ptyxis gnome-terminal kgx konsole xfce4-terminal kitty alacritty wezterm foot xterm".split()
)


def terminal_command(path: Path) -> list[str]:
    """The command that opens a terminal window in path; raises OSError when none is found."""
    if sys.platform == "win32":
        if shutil.which("wt"):  # Windows Terminal, with the user's default profile
            return ["wt", "-w", "new", "-d", str(path)]
        return ["powershell.exe"]
    if sys.platform == "darwin":
        return ["open", "-a", "Terminal", str(path)]
    if own := shlex.split(os.environ.get("TERMINAL", "")):
        return own
    for name in _LINUX_TERMINALS:
        if shutil.which(name):
            return [name]
    raise OSError("no terminal found: set $TERMINAL")


def open_terminal(path: Path) -> None:
    """Open a terminal window in a folder."""
    cmd = terminal_command(path)
    log.debug("terminal %s in %s", cmd, path)
    if sys.platform == "win32" and cmd[0] != "wt":
        # A console of its own, keeping its standard handles: the shell types in it.
        subprocess.Popen(cmd, cwd=path, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    subprocess.Popen(
        cmd,
        cwd=path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_FLAGS,
        start_new_session=sys.platform != "win32",
    )


def launch(*args: str) -> None:
    """Start a detached GUI program (IDE) without waiting for it."""
    cmd = list(args)
    if sys.platform == "win32" and Path(args[0]).suffix.lower() in {".cmd", ".bat"}:
        cmd = ["cmd", "/c", *args]
    log.debug("launch %s", cmd)
    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_FLAGS
    )
