"""Model API keys in the OS credential store with a user-only file fallback.

The keyring account is ``model-profile:<id>``; the fallback directory is the
user configuration folder, restricted to mode 0700/0600 on POSIX. Keys are
never returned together with profile records.
"""
from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path
from typing import Protocol

from platformdirs import user_config_path

DEFAULT_KEYRING_SERVICE = "com.alpha-guard.guardian"
KEY_MIN_CHARS = 8
KEY_MAX_CHARS = 4096


class ModelKeyStoreError(RuntimeError):
    """The model key could not be stored or read safely."""


class _KeyringLike(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...


def _validate_key(value: str) -> str:
    if not isinstance(value, str):
        raise ModelKeyStoreError("model key has an invalid type")
    cleaned = value.strip("\r\n")
    if not KEY_MIN_CHARS <= len(cleaned) <= KEY_MAX_CHARS or cleaned != cleaned.strip():
        raise ModelKeyStoreError("model key is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ModelKeyStoreError("model key is invalid")
    return cleaned


def _optional_keyring() -> _KeyringLike | None:
    try:
        module = importlib.import_module("keyring")
    except (ImportError, ModuleNotFoundError):
        return None
    if not hasattr(module, "get_password") or not hasattr(module, "set_password"):
        return None
    return module


class ModelKeyStore:
    def __init__(
        self,
        *,
        service_name: str = DEFAULT_KEYRING_SERVICE,
        base_dir: str | Path | None = None,
        keyring_backend: _KeyringLike | None = None,
        discover_keyring: bool = True,
    ) -> None:
        self.service_name = service_name
        self._base_dir = Path(base_dir) if base_dir is not None else (
            user_config_path("alpha-guard", appauthor=False) / "model-keys"
        )
        self._keyring_backend = keyring_backend
        self._discover_keyring = discover_keyring

    def _backend(self) -> _KeyringLike | None:
        if self._keyring_backend is not None:
            return self._keyring_backend
        return _optional_keyring() if self._discover_keyring else None

    def save(self, profile_id: str, key: str) -> None:
        value = _validate_key(key)
        account = _account(profile_id)
        backend = self._backend()
        if backend is not None:
            try:
                backend.set_password(self.service_name, account, value)
                if backend.get_password(self.service_name, account) != value:
                    raise ModelKeyStoreError("credential store did not persist the model key")
                return
            except ModelKeyStoreError:
                raise
            except Exception:  # noqa: BLE001 - optional OS backend boundary
                pass
        self._write_file(profile_id, value)

    def load(self, profile_id: str) -> str | None:
        account = _account(profile_id)
        backend = self._backend()
        if backend is not None:
            try:
                value = backend.get_password(self.service_name, account)
                if value is not None:
                    return _validate_key(value)
            except ModelKeyStoreError:
                raise
            except Exception:  # noqa: BLE001 - optional OS backend boundary
                pass
        return self._read_file(profile_id)

    def delete(self, profile_id: str) -> None:
        account = _account(profile_id)
        backend = self._backend()
        if backend is not None:
            delete = getattr(backend, "delete_password", None)
            if callable(delete):
                try:
                    delete(self.service_name, account)
                except Exception:  # noqa: BLE001 - optional OS backend boundary
                    pass
        self._file_path(profile_id).unlink(missing_ok=True)

    def _file_path(self, profile_id: str) -> Path:
        return self._base_dir / f"{_account(profile_id)}.key"

    def _write_file(self, profile_id: str, value: str) -> None:
        directory = self._base_dir
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._restrict_directory(directory)
        descriptor, temporary = tempfile.mkstemp(prefix=".model-key-", dir=directory)
        temporary_path = Path(temporary)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, (value + "\n").encode("utf-8"))
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary_path, self._file_path(profile_id))
            self._restrict_file(self._file_path(profile_id))
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    def _read_file(self, profile_id: str) -> str | None:
        path = self._file_path(profile_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            raw = os.read(descriptor, KEY_MAX_CHARS + 2)
        finally:
            os.close(descriptor)
        try:
            return _validate_key(raw.decode("utf-8").strip("\r\n"))
        except (UnicodeDecodeError, ModelKeyStoreError):
            raise ModelKeyStoreError("model key file is corrupt") from None

    @staticmethod
    def _restrict_directory(path: Path) -> None:
        if os.name == "posix":
            if path.is_symlink():
                raise ModelKeyStoreError("model key directory must not be a symlink")
            os.chmod(path, 0o700)

    @staticmethod
    def _restrict_file(path: Path) -> None:
        if path.is_symlink():
            raise ModelKeyStoreError("model key file must not be a symlink")
        os.chmod(path, 0o600)


def _account(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not profile_id or len(profile_id) > 160 \
            or any(character.isspace() for character in profile_id):
        raise ModelKeyStoreError("model profile id is invalid")
    return f"model-profile:{profile_id}"
