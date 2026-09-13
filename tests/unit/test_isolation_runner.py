from __future__ import annotations

import subprocess
import sys
from signal import SIGTERM
from typing import cast

import pytest

from mishkan.tools.isolation import ContainerOutputLimit, SubprocessRunner


def test_isolation_runner_returns_bounded_text_output() -> None:
    completed = SubprocessRunner(max_output_bytes=128).run(
        (sys.executable, "-c", "print('ready')"),
        5,
    )

    assert completed.returncode == 0
    assert completed.stdout == "ready\n"
    assert completed.stderr == ""


def test_isolation_runner_terminates_on_combined_output_limit() -> None:
    with pytest.raises(ContainerOutputLimit) as raised:
        SubprocessRunner(max_output_bytes=64).run(
            (sys.executable, "-c", "import sys;sys.stdout.write('x' * 4096)"),
            5,
        )

    assert len(raised.value.stdout) > 64


def test_isolation_runner_terminates_on_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessRunner(max_output_bytes=128).run(
            (sys.executable, "-c", "import time;time.sleep(5)"),
            1,
        )


def test_isolation_runner_falls_back_when_group_signal_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 42

        def __init__(self) -> None:
            self.signals: list[int] = []
            self.waits: list[float] = []

        def send_signal(self, signum: int) -> None:
            self.signals.append(signum)

        def wait(self, *, timeout: float) -> int:
            self.waits.append(timeout)
            return 0

    process = Process()

    def deny_group_signal(_pid: int, _signum: int) -> None:
        raise PermissionError("group signal denied")

    monkeypatch.setattr("mishkan.tools.isolation.os.killpg", deny_group_signal)

    SubprocessRunner._terminate(cast(subprocess.Popen[bytes], process))

    assert process.signals == [SIGTERM]
    assert process.waits == [0.5]
