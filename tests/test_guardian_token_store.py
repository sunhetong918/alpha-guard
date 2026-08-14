from __future__ import annotations

import os
import stat

import pytest

from guardian.token_store import (
    GuardianTokenStore,
    TokenStoreError,
    validate_token,
)


class _MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


class _BrokenKeyring:
    def get_password(self, service_name: str, username: str) -> str | None:
        del service_name, username
        raise RuntimeError("backend unavailable")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        del service_name, username, password
        raise RuntimeError("backend unavailable")


def test_keyring_is_preferred_and_file_is_not_created(tmp_path) -> None:
    keyring = _MemoryKeyring()
    path = tmp_path / "guardian.token"
    store = GuardianTokenStore(fallback_path=path, keyring_backend=keyring)

    first = store.load_or_create()
    second = store.load_or_create()

    assert first == second
    assert len(first) >= 43
    assert not path.exists()


def test_broken_keyring_falls_back_to_stable_mode_0600_file(tmp_path) -> None:
    path = tmp_path / "private" / "guardian.token"
    store = GuardianTokenStore(
        fallback_path=path,
        keyring_backend=_BrokenKeyring(),
    )

    first = store.load_or_create()
    second = store.load_or_create()

    assert first == second
    assert validate_token(first) == first
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_existing_overpermissive_file_is_restricted(tmp_path) -> None:
    path = tmp_path / "guardian.token"
    token = "a" * 43
    path.write_text(token + "\n", encoding="ascii")
    path.chmod(0o644)

    loaded = GuardianTokenStore(
        fallback_path=path,
        discover_keyring=False,
    ).load_or_create()

    assert loaded == token
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_existing_token_fails_closed(tmp_path) -> None:
    path = tmp_path / "guardian.token"
    path.write_text("too-short\n", encoding="ascii")

    with pytest.raises(TokenStoreError, match="corrupt"):
        GuardianTokenStore(
            fallback_path=path,
            discover_keyring=False,
        ).load_or_create()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink contract")
def test_fallback_never_follows_token_symlink(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("a" * 43, encoding="ascii")
    path = tmp_path / "guardian.token"
    path.symlink_to(target)

    with pytest.raises(TokenStoreError, match="symlink"):
        GuardianTokenStore(
            fallback_path=path,
            discover_keyring=False,
        ).load_or_create()


def test_rotate_replaces_fallback_token(tmp_path) -> None:
    path = tmp_path / "guardian.token"
    store = GuardianTokenStore(fallback_path=path, discover_keyring=False)
    before = store.load_or_create()

    after = store.rotate()

    assert after != before
    assert store.load_or_create() == after
