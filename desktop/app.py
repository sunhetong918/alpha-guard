"""Installed entry point for the optional Alpha Guard desktop application."""

from __future__ import annotations

import sys


def main() -> int:
    """Load the optional Qt application only when the desktop command runs."""

    try:
        from .ui.app import run
    except (ImportError, ModuleNotFoundError) as exc:
        if exc.name is not None and (
            exc.name == "PySide6" or exc.name.startswith("PySide6.")
        ):
            print(
                "Alpha Guard desktop support is not installed; "
                "install the desktop extra.",
                file=sys.stderr,
            )
            return 2
        raise
    return run()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
