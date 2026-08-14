from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from guardian.autostart.macos import (
    LaunchAgentSpec,
    build_launch_agent_payload,
    launch_agent_path,
    render_launch_agent,
)
from guardian.autostart.windows import (
    WindowsAutostartError,
    build_run_command,
    hkcu_run_matches,
    run_value_name,
)


def test_launch_agent_runs_guardian_in_foreground_and_throttles_restarts(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "Alpha Guard.app" / "Contents" / "MacOS" / "guardian")
    spec = LaunchAgentSpec(executable=executable)

    payload = build_launch_agent_payload(spec)

    assert payload["ProgramArguments"] == [str(executable), "guardian"]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ProcessType"] == "Background"
    assert payload["ThrottleInterval"] >= 10
    assert "EnvironmentVariables" not in payload
    assert plistlib.loads(render_launch_agent(spec)) == payload


def test_launch_agent_path_is_per_user() -> None:
    path = launch_agent_path(home="/Users/example")
    assert path == Path(
        "/Users/example/Library/LaunchAgents/com.alpha-guard.guardian.plist"
    )


def test_launch_agent_rejects_relative_executable_and_control_characters() -> None:
    with pytest.raises(ValueError, match="absolute"):
        build_launch_agent_payload(LaunchAgentSpec(executable=Path("guardian")))
    with pytest.raises(ValueError, match="argument"):
        build_launch_agent_payload(
            LaunchAgentSpec(executable=Path("/Applications/guardian"), arguments=("x\n",))
        )


def test_windows_run_command_quotes_executable_and_has_stable_value_name() -> None:
    command = build_run_command(
        r"C:\Program Files\Alpha Guard\guardian.exe",
        ("guardian", "--profile", "default"),
    )

    assert command.startswith('"C:\\Program Files\\Alpha Guard\\guardian.exe"')
    assert command.endswith("guardian --profile default")
    assert run_value_name() == run_value_name()
    assert run_value_name(profile="paper") != run_value_name(profile="default")


def test_windows_run_command_rejects_relative_or_oversized_values() -> None:
    with pytest.raises(ValueError, match="absolute"):
        build_run_command("guardian.exe")
    with pytest.raises(ValueError, match="260"):
        build_run_command(
            r"C:\AlphaGuard\guardian.exe",
            ("x" * 250,),
        )


@pytest.mark.skipif(os.name == "nt", reason="non-Windows lazy import contract")
def test_windows_registry_dependency_is_delayed_and_actionable() -> None:
    with pytest.raises(WindowsAutostartError, match="unavailable"):
        hkcu_run_matches(run_value_name(), r"C:\AlphaGuard\guardian.exe guardian")
