"""User-visible desktop preferences owned by the Guardian.

Secrets and trading rules never cross this store.  The document contains only
presentation and operating-system integration preferences that the desktop UI
is allowed to edit.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platformdirs import user_config_path
from pydantic import BaseModel, ConfigDict, Field, model_validator

_CLOCK = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class DesktopPreferences(BaseModel):
    """Strict public settings accepted from the local desktop client."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    language: str = Field(default="zh-CN", pattern=r"^[a-z]{2}-[A-Z]{2}$")
    launch_at_login: bool = False
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:30"

    @model_validator(mode="after")
    def validate_public_preferences(self) -> DesktopPreferences:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unsupported timezone") from None
        if not _CLOCK.fullmatch(self.quiet_hours_start) or not _CLOCK.fullmatch(
            self.quiet_hours_end
        ):
            raise ValueError("quiet hours must use HH:MM")
        if self.quiet_hours_start == self.quiet_hours_end:
            raise ValueError("quiet hours must have a non-zero duration")
        return self


@dataclass(frozen=True, slots=True)
class PreferencesDocument:
    revision: int
    preferences: DesktopPreferences


class PreferencesConflictError(RuntimeError):
    """The UI attempted to overwrite a newer local preferences revision."""


class PreferencesStoreError(RuntimeError):
    """Preferences could not be read or stored safely."""


class PreferencesStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(
            path
            or (
                user_config_path("alpha-guard", appauthor=False)
                / "desktop-preferences.json"
            )
        ).expanduser()

    def load(self) -> PreferencesDocument:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return PreferencesDocument(0, DesktopPreferences())
        except OSError as exc:
            raise PreferencesStoreError("preferences are unavailable") from exc
        if len(raw) > 16_384:
            raise PreferencesStoreError("preferences are corrupt")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PreferencesStoreError("preferences are corrupt") from None
        if not isinstance(payload, dict) or set(payload) != {
            "revision",
            "preferences",
        }:
            raise PreferencesStoreError("preferences are corrupt")
        revision = payload["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise PreferencesStoreError("preferences are corrupt")
        try:
            preferences = DesktopPreferences.model_validate(payload["preferences"])
        except Exception:
            raise PreferencesStoreError("preferences are corrupt") from None
        return PreferencesDocument(revision, preferences)

    def apply(
        self,
        preferences: DesktopPreferences,
        *,
        expected_revision: int,
    ) -> PreferencesDocument:
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        current = self.load()
        if current.revision != expected_revision:
            raise PreferencesConflictError("preferences revision changed")
        document = PreferencesDocument(current.revision + 1, preferences)
        self._atomic_write(document)
        return document

    def _atomic_write(self, document: PreferencesDocument) -> None:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or self.path.is_symlink():
            raise PreferencesStoreError("preferences path is unsafe")
        if os.name == "posix":
            os.chmod(parent, 0o700)
        body = json.dumps(
            {
                "revision": document.revision,
                "preferences": document.preferences.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".desktop-preferences-",
            dir=parent,
        )
        temporary = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            os.write(descriptor, body)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.path)
            if os.name == "posix":
                os.chmod(self.path, 0o600)
        except OSError as exc:
            raise PreferencesStoreError("preferences could not be saved") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "DesktopPreferences",
    "PreferencesConflictError",
    "PreferencesDocument",
    "PreferencesStore",
    "PreferencesStoreError",
]
