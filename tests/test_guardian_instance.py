from __future__ import annotations

import pytest

from guardian.instance import build_instance_names


def test_instance_names_are_stable_short_and_role_separated() -> None:
    first = build_instance_names(user_scope="uid-501")
    second = build_instance_names(user_scope="uid-501")

    assert first == second
    assert first.guardian != first.desktop
    assert len(first.guardian) <= 96
    assert len(first.desktop) <= 96
    assert "uid-501" not in first.guardian


def test_profile_and_user_scope_isolate_instances() -> None:
    default = build_instance_names(profile="default", user_scope="uid-501")
    other_profile = build_instance_names(profile="paper", user_scope="uid-501")
    other_user = build_instance_names(profile="default", user_scope="uid-502")

    assert len({default.guardian, other_profile.guardian, other_user.guardian}) == 3


@pytest.mark.parametrize("value", ["", "../escape", "has space", "x" * 129])
def test_instance_identifiers_are_strict(value: str) -> None:
    with pytest.raises(ValueError):
        build_instance_names(profile=value, user_scope="uid-501")
