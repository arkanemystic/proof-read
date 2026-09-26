"""W2 test helpers: a FakeSandbox whose run() really executes the command in a temp dir."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from proofread.contracts import WORKSPACE, FakeSandbox


def exec_handler(timeout_s: float = 30.0):
    def handler(cmd: str, files: dict[str, str]):
        with tempfile.TemporaryDirectory() as d:
            for p, c in files.items():
                rel = p[len(WORKSPACE) + 1:] if p.startswith(WORKSPACE + "/") else p.lstrip("/")
                f = Path(d) / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(c)
            cmd = cmd.replace("python ", f"{sys.executable} ", 1) if cmd.startswith("python ") else cmd
            try:
                r = subprocess.run(cmd, shell=True, cwd=d, capture_output=True, text=True, timeout=timeout_s,
                                   env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
            except subprocess.TimeoutExpired:
                return 124, "timeout"
            return r.returncode, r.stdout + r.stderr
    return handler


class ExecSandbox(FakeSandbox):
    """FakeSandbox + real execution; tracks start/stop for lifecycle assertions."""

    instances: list["ExecSandbox"] = []

    def __init__(self, episode_id: str = "ep-exec", timeout_s: float = 30.0) -> None:
        super().__init__(episode_id, handler=exec_handler(timeout_s))
        self.started = False
        self.stopped = False
        ExecSandbox.instances.append(self)

    async def start(self, task) -> None:
        await super().start(task)
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def exec_sandbox_cls():
    ExecSandbox.instances.clear()
    return ExecSandbox
