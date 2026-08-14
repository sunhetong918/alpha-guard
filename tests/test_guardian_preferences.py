from __future__ import annotations

import json

import pytest

from guardian.preferences import (
    DesktopPreferences,
    PreferencesConflictError,
    PreferencesStore,
    PreferencesStoreError,
)


def test_preferences_round_trip_and_revision_conflict(tmp_path) -> None:
    store = PreferencesStore(tmp_path / "preferences.json")
    assert store.load().revision == 0

    first = store.apply(
        DesktopPreferences(
            timezone="America/New_York",
            language="en-US",
            launch_at_login=True,
            quiet_hours_enabled=True,
            quiet_hours_start="22:30",
            quiet_hours_end="07:00",
        ),
        expected_revision=0,
    )
    assert first.revision == 1
    assert store.load() == first
    with pytest.raises(PreferencesConflictError):
        store.apply(DesktopPreferences(), expected_revision=0)


@pytest.mark.parametrize(
    "update",
    [
        {"timezone": "Mars/Olympus"},
        {"quiet_hours_start": "25:00"},
        {"quiet_hours_start": "07:30", "quiet_hours_end": "07:30"},
        {"language": "zh_cn"},
    ],
)
def test_preferences_reject_invalid_public_values(update) -> None:
    with pytest.raises(ValueError):
        DesktopPreferences(**update)


def test_preferences_fail_closed_without_returning_raw_payload(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    secret = "https://example.test/private-preference-token"
    path.write_text(json.dumps({"revision": 1, "preferences": secret}))

    with pytest.raises(PreferencesStoreError) as captured:
        PreferencesStore(path).load()
    assert secret not in str(captured.value)


def test_preferences_file_is_private_on_posix(tmp_path) -> None:
    path = tmp_path / "private" / "preferences.json"
    PreferencesStore(path).apply(DesktopPreferences(), expected_revision=0)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
