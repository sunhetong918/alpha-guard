"""Release-artifact tests that never import from the source checkout."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from email.parser import BytesParser
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIRECTORIES = (
    "analysis",
    "data",
    "desktop",
    "guardian",
    "news",
    "notifier",
    "reliability",
    "signals",
    "state",
)
TOP_LEVEL_BUILD_FILES = (
    "README.md",
    "config.py",
    "main.py",
    "pyproject.toml",
    "scheduler.py",
)
DESKTOP_RELEASE_WORKFLOW = PROJECT_ROOT / ".github/workflows/desktop-release.yml"


def _run(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 120,
) -> subprocess.CompletedProcess[str]:
    rendered = [os.fspath(part) for part in command]
    completed = subprocess.run(
        rendered,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    assert completed.returncode == 0, (
        f"command failed ({completed.returncode}): {' '.join(rendered)}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    staging = tmp_path_factory.mktemp("wheel-staging")
    source = staging / "source"
    output = staging / "wheel"
    source.mkdir()
    output.mkdir()
    for filename in TOP_LEVEL_BUILD_FILES:
        shutil.copy2(PROJECT_ROOT / filename, source / filename)
    for package in PACKAGE_DIRECTORIES:
        shutil.copytree(
            PROJECT_ROOT / package,
            source / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    _run(
        (
            "uv",
            "build",
            "--wheel",
            "--offline",
            "--no-build-logs",
            "--out-dir",
            output,
        ),
        cwd=source,
    )
    wheels = list(output.glob("alpha_guard-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found: {wheels}"
    return wheels[0]


def test_wheel_contains_runtime_modules_and_packages(built_wheel: Path) -> None:
    expected = {
        "config.py",
        "desktop/__init__.py",
        "desktop/app.py",
        "desktop/assets/AlphaGuard.icns",
        "desktop/assets/alpha-guard-icon-master.png",
        "desktop/ui/__init__.py",
        "desktop/ui/app.py",
        "desktop/ui/fixtures/guardian.json",
        "guardian/__init__.py",
        "guardian/protocol.py",
        "guardian/service.py",
        "main.py",
        "scheduler.py",
        "reliability/__init__.py",
        "reliability/freshness.py",
        "reliability/models.py",
        "reliability/provider.py",
        "state/__init__.py",
        "state/blindness.py",
        "state/cockpit.py",
        "state/contract.py",
        "state/store.py",
    }
    with zipfile.ZipFile(built_wheel) as wheel:
        names = set(wheel.namelist())
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = wheel.read(entry_points_name).decode()
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(wheel.read(metadata_name))

    assert expected <= names
    assert "alpha-guard = main:app" in entry_points
    assert "alpha-guard-desktop = desktop.app:main" in entry_points
    assert "alpha-guard-guardian = guardian.service:main" in entry_points
    assert {"desktop", "desktop-build"} <= set(
        metadata.get_all("Provides-Extra", [])
    )
    requirements = metadata.get_all("Requires-Dist", [])
    assert any(
        requirement.startswith("PySide6") and 'extra == "desktop"' in requirement
        for requirement in requirements
    )
    assert any(
        requirement.startswith("keyring") and 'extra == "desktop"' in requirement
        for requirement in requirements
    )
    assert any(
        requirement.startswith("PyInstaller==6.21.0")
        and 'extra == "desktop-build"' in requirement
        for requirement in requirements
    )


def test_native_release_freezes_installed_wheel_not_editable_checkout() -> None:
    workflow = DESKTOP_RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "--no-install-project" in workflow
    assert "uv build \\\n" in workflow
    assert "uv pip install \\\n" in workflow
    assert "--python .venv" in workflow
    assert "--no-deps" in workflow
    assert 'assert "archive_info" in metadata' in workflow
    assert "is_relative_to(environment)" in workflow
    assert "uv run --no-sync pyinstaller" in workflow
    assert '--icon "desktop/assets/AlphaGuard.icns"' in workflow
    assert re.search(r"uv run(?! --no-sync) pyinstaller", workflow) is None
    assert (
        "dist/AlphaGuard-Desktop.app/Contents/MacOS/AlphaGuard-Desktop --help"
        in workflow
    )
    assert "dist/alpha-guard-guardian/alpha-guard-guardian --help" in workflow


def test_wheel_installs_and_runs_outside_source_tree(
    built_wheel: Path,
    tmp_path: Path,
) -> None:
    environment = tmp_path / "venv"
    outside = tmp_path / "outside-source-tree"
    outside.mkdir()

    _run(
        (
            "uv",
            "venv",
            "--offline",
            "--python",
            sys.executable,
            environment,
        ),
        cwd=outside,
    )
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    alpha_guard = scripts / ("alpha-guard.exe" if os.name == "nt" else "alpha-guard")
    desktop = scripts / (
        "alpha-guard-desktop.exe" if os.name == "nt" else "alpha-guard-desktop"
    )
    guardian = scripts / (
        "alpha-guard-guardian.exe" if os.name == "nt" else "alpha-guard-guardian"
    )
    _run(
        (
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            python,
            f"{built_wheel}[desktop]",
        ),
        cwd=outside,
    )
    _run(("uv", "pip", "check", "--python", python), cwd=outside)

    clean_env = os.environ.copy()
    for variable in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        clean_env.pop(variable, None)
    clean_env["PYTHONNOUSERSITE"] = "1"
    clean_env["PYTHONSAFEPATH"] = "1"
    imported = _run(
        (
            python,
            "-I",
            "-c",
            (
                "import json, main, reliability, state, desktop, desktop.app, "
                "desktop.ui, guardian, guardian.service; "
                "print(json.dumps([main.__file__, reliability.__file__, "
                "state.__file__, desktop.__file__, desktop.app.__file__, "
                "desktop.ui.__file__, guardian.__file__, guardian.service.__file__]))"
            ),
        ),
        cwd=outside,
        env=clean_env,
    )
    imported_paths = [Path(value).resolve() for value in json.loads(imported.stdout)]
    assert all(path.is_relative_to(environment.resolve()) for path in imported_paths)
    assert all(not path.is_relative_to(PROJECT_ROOT) for path in imported_paths)

    help_result = _run(
        (alpha_guard, "--help"),
        cwd=outside,
        env=clean_env,
        timeout_seconds=10,
    )
    help_text = help_result.stdout + help_result.stderr
    assert "alpha-guard" in help_text
    assert "Usage" in help_text

    clean_env["QT_QPA_PLATFORM"] = "offscreen"
    guardian_help = _run(
        (guardian, "--help"),
        cwd=outside,
        env=clean_env,
        timeout_seconds=10,
    )
    assert "Guardian" in guardian_help.stdout + guardian_help.stderr

    desktop_help = _run(
        (desktop, "--help"),
        cwd=outside,
        env=clean_env,
        timeout_seconds=10,
    )
    desktop_help_text = desktop_help.stdout + desktop_help.stderr
    assert "alpha-guard-desktop" in desktop_help_text
    assert "--demo" in desktop_help_text
