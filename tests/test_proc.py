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
