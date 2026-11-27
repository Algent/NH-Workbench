import asyncio
import sys

import pytest

from nh_workbench import proc


async def test_cancelled_run_kills_and_reaps_the_process(tmp_path, monkeypatch):
    started = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        started.append(await real(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    task = asyncio.create_task(proc.run(sys.executable, "-c", "import time; time.sleep(60)"))
    for _ in range(200):
        if started:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)  # let it reach communicate()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started and started[0].returncode is not None  # killed and waited for, not left running


def _alive(pid: int) -> bool:
    import os
    import subprocess

    if sys.platform == "win32":
        found = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in found.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def test_cancelled_run_kills_what_the_process_started(tmp_path):
    """Like Git for Windows' git.exe, a launcher whose child holds the pipes: both go, and the task ends."""
    pid_file = tmp_path / "child.pid"
    launcher = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pid_file)!r}, 'w').write(str(child.pid)); "
        "time.sleep(60)"
    )
    task = asyncio.create_task(proc.run(sys.executable, "-c", launcher))
    for _ in range(250):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.02)
    child = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    for _ in range(100):
        if not _alive(child):
            break
        await asyncio.sleep(0.05)
    assert not _alive(child)


def test_terminal_command_per_platform(tmp_path, monkeypatch):
    found: set[str] = set()
    monkeypatch.setattr(proc.shutil, "which", lambda name: name if name in found else None)
    monkeypatch.setattr(proc.sys, "platform", "win32")
    assert proc.terminal_command(tmp_path) == ["powershell.exe"]
    found.add("wt")
    assert proc.terminal_command(tmp_path) == ["wt", "-w", "new", "-d", str(tmp_path)]

    monkeypatch.setattr(proc.sys, "platform", "darwin")
    assert proc.terminal_command(tmp_path) == ["open", "-a", "Terminal", str(tmp_path)]

    monkeypatch.setattr(proc.sys, "platform", "linux")
    monkeypatch.delenv("TERMINAL", raising=False)
    with pytest.raises(OSError, match="TERMINAL"):
        proc.terminal_command(tmp_path)
    found.update({"konsole", "xterm"})
    assert proc.terminal_command(tmp_path) == ["konsole"]
    monkeypatch.setenv("TERMINAL", "kitty --single-instance")
    assert proc.terminal_command(tmp_path) == ["kitty", "--single-instance"]
