"""Internal execution gate used to persist process identity before target exec."""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        return 125
    try:
        control_descriptor = int(sys.argv[1])
    except ValueError:
        return 125
    executable = sys.argv[2]
    try:
        release = os.read(control_descriptor, 1)
    finally:
        os.close(control_descriptor)
    if release != b"1":
        return 125
    try:
        os.execve(executable, [executable, *sys.argv[3:]], os.environ)
    except OSError as exc:
        message = f"mishkan launcher could not execute target: {exc.strerror or 'OS error'}\n"
        os.write(2, message.encode(errors="replace"))
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
