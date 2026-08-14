"""macOS per-user LaunchAgent registration for Guardian.

The generated agent keeps Guardian in the foreground and asks ``launchd`` to
restart it only after unsuccessful exit.  A clean ``guardian.stop`` can
therefore remain stopped until the next login or explicit bootstrap.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_LABEL = "com.alpha-guard.guardian"
DEFAULT_THROTTLE_SECONDS = 30
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")


class MacOSAutostartError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LaunchAgentSpec:
    executable: Path
    arguments: tuple[str, ...] = ("guardian",)
    label: str = DEFAULT_LABEL
    throttle_interval_seconds: int = DEFAULT_THROTTLE_SECONDS
    stdout_path: Path | None = None
    stderr_path: Path | None = None


def build_launch_agent_payload(spec: LaunchAgentSpec) -> dict[str, Any]:
    executable = _absolute_path(spec.executable, "executable")
    label = _validated_label(spec.label)
    if not 10 <= spec.throttle_interval_seconds <= 3_600:
        raise ValueError("throttle_interval_seconds must be between 10 and 3600")
    arguments = tuple(_validated_argument(value) for value in spec.arguments)
    payload: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [str(executable), *arguments],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "ThrottleInterval": spec.throttle_interval_seconds,
    }
    if spec.stdout_path is not None:
        payload["StandardOutPath"] = str(
            _absolute_path(spec.stdout_path, "stdout_path")
        )
    if spec.stderr_path is not None:
        payload["StandardErrorPath"] = str(
            _absolute_path(spec.stderr_path, "stderr_path")
        )
    return payload


def render_launch_agent(spec: LaunchAgentSpec) -> bytes:
    return plistlib.dumps(
        build_launch_agent_payload(spec),
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )


def launch_agent_path(
    label: str = DEFAULT_LABEL,
    *,
    home: str | Path | None = None,
) -> Path:
    label = _validated_label(label)
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / "Library" / "LaunchAgents" / f"{label}.plist"


def install_launch_agent(
    spec: LaunchAgentSpec,
    *,
    home: str | Path | None = None,
) -> Path:
    """Atomically install the plist without bootstrapping it."""

    destination = launch_agent_path(spec.label, home=home)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{spec.label}.",
        suffix=".plist",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        body = render_launch_agent(spec)
        os.write(descriptor, body)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        os.chmod(destination, 0o644)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def enable_launch_agent(
    spec: LaunchAgentSpec,
    *,
    home: str | Path | None = None,
) -> Path:
    if os.name != "posix" or not hasattr(os, "getuid"):
        raise MacOSAutostartError("macOS LaunchAgent registration is unavailable")
    destination = install_launch_agent(spec, home=home)
    domain = f"gui/{os.getuid()}"
    completed = subprocess.run(
        ["launchctl", "bootstrap", domain, str(destination)],
        check=False,
        capture_output=True,
        text=False,
    )
    if completed.returncode != 0:
        raise MacOSAutostartError("launchctl bootstrap failed")
    return destination


def disable_launch_agent(
    label: str = DEFAULT_LABEL,
    *,
    home: str | Path | None = None,
    remove_plist: bool = True,
) -> None:
    destination = launch_agent_path(label, home=home)
    if os.name != "posix" or not hasattr(os, "getuid"):
        raise MacOSAutostartError("macOS LaunchAgent registration is unavailable")
    domain = f"gui/{os.getuid()}"
    # Bootout is idempotent from the product perspective; a missing job should
    # not block removal of a stale per-user plist.
    subprocess.run(
        ["launchctl", "bootout", domain, str(destination)],
        check=False,
        capture_output=True,
        text=False,
    )
    if remove_plist:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass


def _validated_label(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("label must be a string")
    if not _LABEL_PATTERN.fullmatch(value) or "." not in value:
        raise ValueError("LaunchAgent label is invalid")
    return value


def _validated_argument(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("LaunchAgent arguments must be strings")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("LaunchAgent argument is invalid")
    return value


def _absolute_path(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError(f"{label} must be an absolute path")
    return path


__all__ = [
    "DEFAULT_LABEL",
    "LaunchAgentSpec",
    "MacOSAutostartError",
    "build_launch_agent_payload",
    "disable_launch_agent",
    "enable_launch_agent",
    "install_launch_agent",
    "launch_agent_path",
    "render_launch_agent",
]
