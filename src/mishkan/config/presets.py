"""Inspectable packaged configuration preset access."""

from importlib.resources import files
from pathlib import Path

from mishkan.domain.errors import ErrorCode, MishkanError

PRESET_NAMES = ("local", "cloud", "hybrid")


def preset_text(name: str) -> str:
    if name not in PRESET_NAMES:
        raise MishkanError(
            ErrorCode.CONFIGURATION,
            "unknown configuration preset",
            details={"preset": name, "available": list(PRESET_NAMES)},
        )
    resource = files("mishkan.resources.presets").joinpath(f"{name}.yaml")
    return resource.read_text(encoding="utf-8")


def write_preset(name: str, output: Path, *, overwrite: bool = False) -> Path:
    target = output.expanduser().resolve()
    if target.exists() and not overwrite:
        raise MishkanError(
            ErrorCode.CONFIGURATION,
            "configuration output already exists",
            details={"output": str(target)},
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(preset_text(name), encoding="utf-8")
    return target
