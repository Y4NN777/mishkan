from __future__ import annotations

import subprocess
import sys

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
