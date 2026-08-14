"""Windows per-user HKCU Run registration for Guardian."""

from __future__ import annotations

import hashlib
import importlib
import re
import subprocess
from pathlib import PureWindowsPath
from typing import Any

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAX_RUN_COMMAND_CHARS = 260
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class WindowsAutostartError(RuntimeError):
    pass


def run_value_name(
    application_id: str = "com.alpha-guard",
    profile: str = "default",
) -> str:
    application_id = _validated_identifier(application_id, "application_id")
    profile = _validated_identifier(profile, "profile")
    digest = hashlib.sha256(
        f"{application_id}\0{profile}".encode("utf-8")
    ).hexdigest()[:12]
    return f"AlphaGuardGuardian-{digest}"


def build_run_command(
    executable: str | PureWindowsPath,
    arguments: tuple[str, ...] = ("guardian",),
) -> str:
    path = PureWindowsPath(executable)
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("Guardian executable must be an absolute Windows path")
    validated_arguments = [_validated_argument(value) for value in arguments]
    command = subprocess.list2cmdline([str(path), *validated_arguments])
    if len(command) > MAX_RUN_COMMAND_CHARS:
        raise ValueError("Windows Run command exceeds 260 characters")
    return command


def enable_hkcu_run(value_name: str, command: str) -> None:
    module = _winreg()
    value_name = _validated_value_name(value_name)
    command = _validated_command(command)
    try:
        with module.CreateKeyEx(
            module.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            module.KEY_SET_VALUE,
        ) as key:
            module.SetValueEx(key, value_name, 0, module.REG_SZ, command)
    except OSError as exc:
        raise WindowsAutostartError("could not enable Guardian autostart") from exc


def disable_hkcu_run(value_name: str) -> None:
    module = _winreg()
    value_name = _validated_value_name(value_name)
    try:
        with module.OpenKey(
            module.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            module.KEY_SET_VALUE,
        ) as key:
            try:
                module.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WindowsAutostartError("could not disable Guardian autostart") from exc


def hkcu_run_matches(value_name: str, expected_command: str) -> bool:
    module = _winreg()
    value_name = _validated_value_name(value_name)
    expected_command = _validated_command(expected_command)
    try:
        with module.OpenKey(
            module.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            module.KEY_READ,
        ) as key:
            value, value_type = module.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WindowsAutostartError("could not inspect Guardian autostart") from exc
    return value_type == module.REG_SZ and value == expected_command


def _winreg() -> Any:
    try:
        return importlib.import_module("winreg")
    except (ImportError, ModuleNotFoundError) as exc:
        raise WindowsAutostartError(
            "Windows HKCU autostart is unavailable on this platform"
        ) from exc


def _validated_identifier(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _validated_argument(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Guardian arguments must be strings")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("Guardian argument is invalid")
    return value


def _validated_value_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("registry value name must be a string")
    if not value or len(value) > 160 or "\x00" in value:
        raise ValueError("registry value name is invalid")
    return value


def _validated_command(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("registry command must be a string")
    if (
        not value
        or len(value) > MAX_RUN_COMMAND_CHARS
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError("registry command is invalid")
    return value


__all__ = [
    "MAX_RUN_COMMAND_CHARS",
    "RUN_KEY",
    "WindowsAutostartError",
    "build_run_command",
    "disable_hkcu_run",
    "enable_hkcu_run",
    "hkcu_run_matches",
    "run_value_name",
]
