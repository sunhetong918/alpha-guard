"""Per-user Guardian IPC token storage.

The OS credential store is preferred when the optional ``keyring`` package has
an operational backend.  A local file is the deterministic fallback.  On
POSIX systems both its parent directory and file are forced to user-only
permissions; symlinks are never followed.
"""

from __future__ import annotations

import importlib
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Protocol

from platformdirs import user_config_path

TOKEN_BYTES = 32
TOKEN_MIN_CHARS = 32
TOKEN_MAX_CHARS = 256
DEFAULT_KEYRING_SERVICE = "com.alpha-guard.guardian"
DEFAULT_KEYRING_ACCOUNT = "ipc-token"


class TokenStoreError(RuntimeError):
    """The Guardian token could not be loaded or stored safely."""


class _KeyringLike(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(
        self, service_name: str, username: str, password: str
    ) -> None: ...


class GuardianTokenStore:
    def __init__(
        self,
        *,
        service_name: str = DEFAULT_KEYRING_SERVICE,
        account_name: str = DEFAULT_KEYRING_ACCOUNT,
        fallback_path: str | Path | None = None,
        keyring_backend: _KeyringLike | None = None,
        discover_keyring: bool = True,
    ) -> None:
        self.service_name = _validated_identifier(service_name, "service_name")
        self.account_name = _validated_identifier(account_name, "account_name")
        self.fallback_path = Path(
            fallback_path
            or (user_config_path("alpha-guard", appauthor=False) / "guardian.token")
        ).expanduser()
        self._keyring_backend = keyring_backend
        self._discover_keyring = discover_keyring

    def load_or_create(self) -> str:
        backend = self._keyring_backend
        if backend is None and self._discover_keyring:
            backend = _optional_keyring()
        if backend is not None:
            try:
                existing = backend.get_password(self.service_name, self.account_name)
                if existing is not None:
                    return validate_token(existing)
                generated = generate_token()
                backend.set_password(
                    self.service_name,
                    self.account_name,
                    generated,
                )
                stored = backend.get_password(self.service_name, self.account_name)
                if stored is None:
                    raise TokenStoreError("credential store did not persist token")
                return validate_token(stored)
            except TokenStoreError:
                raise
            except Exception:  # noqa: BLE001 - optional OS backend boundary
                # An installed keyring package does not guarantee an operational
                # Keychain/Credential Manager backend.  Fall back without
                # returning backend exception text, which can contain metadata.
                pass
        return self._load_or_create_file()

    def rotate(self) -> str:
        token = generate_token()
        backend = self._keyring_backend
        if backend is None and self._discover_keyring:
            backend = _optional_keyring()
        if backend is not None:
            try:
                backend.set_password(self.service_name, self.account_name, token)
                stored = backend.get_password(self.service_name, self.account_name)
                if stored is None:
                    raise TokenStoreError("credential store did not persist token")
                return validate_token(stored)
            except TokenStoreError:
                raise
            except Exception:  # noqa: BLE001 - optional OS backend boundary
                pass
        self._atomic_write_file(token)
        return token

    def _load_or_create_file(self) -> str:
        parent = self.fallback_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _restrict_directory(parent)
        try:
            descriptor = _open_token_file(
                self.fallback_path,
                os.O_RDONLY,
            )
        except FileNotFoundError:
            token = generate_token()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                descriptor = _open_token_file(self.fallback_path, flags, 0o600)
            except FileExistsError:
                # Another local process won the create race.  Read and validate
                # its complete token rather than replacing it.
                descriptor = _open_token_file(self.fallback_path, os.O_RDONLY)
            else:
                try:
                    os.write(descriptor, (token + "\n").encode("ascii"))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _restrict_file(self.fallback_path)
                return token

        try:
            raw = os.read(descriptor, TOKEN_MAX_CHARS + 2)
        finally:
            os.close(descriptor)
        _restrict_file(self.fallback_path)
        try:
            value = raw.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise TokenStoreError("Guardian token file is corrupt") from exc
        return validate_token(value)

    def _atomic_write_file(self, token: str) -> None:
        token = validate_token(token)
        parent = self.fallback_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _restrict_directory(parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".guardian-token-",
            dir=parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, (token + "\n").encode("ascii"))
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary_path, self.fallback_path)
            _restrict_file(self.fallback_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def validate_token(value: str) -> str:
    if not isinstance(value, str):
        raise TokenStoreError("Guardian token has an invalid type")
    if value != value.strip() or not TOKEN_MIN_CHARS <= len(value) <= TOKEN_MAX_CHARS:
        raise TokenStoreError("Guardian token is corrupt")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise TokenStoreError("Guardian token is corrupt") from exc
    allowed = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(character not in allowed for character in encoded):
        raise TokenStoreError("Guardian token is corrupt")
    return value


def _optional_keyring() -> _KeyringLike | None:
    try:
        module = importlib.import_module("keyring")
    except (ImportError, ModuleNotFoundError):
        return None
    if not hasattr(module, "get_password") or not hasattr(module, "set_password"):
        return None
    return module


def _open_token_file(path: Path, flags: int, mode: int = 0o600) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags | no_follow, mode)
    except OSError as exc:
        if path.is_symlink():
            raise TokenStoreError("Guardian token path must not be a symlink") from exc
        raise


def _restrict_directory(path: Path) -> None:
    if os.name == "posix":
        if path.is_symlink():
            raise TokenStoreError("Guardian token directory must not be a symlink")
        os.chmod(path, 0o700)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise TokenStoreError("Guardian token directory is not user-only")


def _restrict_file(path: Path) -> None:
    if path.is_symlink():
        raise TokenStoreError("Guardian token path must not be a symlink")
    os.chmod(path, 0o600)
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise TokenStoreError("Guardian token file is not mode 0600")


def _validated_identifier(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    stripped = value.strip()
    if stripped != value or not 1 <= len(value) <= 160:
        raise ValueError(f"{label} is invalid")
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise ValueError(f"{label} is invalid")
    return value


__all__ = [
    "DEFAULT_KEYRING_ACCOUNT",
    "DEFAULT_KEYRING_SERVICE",
    "GuardianTokenStore",
    "TokenStoreError",
    "generate_token",
    "validate_token",
]
