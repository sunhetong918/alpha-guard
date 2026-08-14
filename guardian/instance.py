"""Stable per-user names for Guardian and desktop single-instance sockets."""

from __future__ import annotations

import getpass
import hashlib
import os
import re
from dataclasses import dataclass

DEFAULT_APPLICATION_ID = "com.alpha-guard"
DEFAULT_PROFILE = "default"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class InstanceNames:
    guardian: str
    desktop: str


def build_instance_names(
    *,
    application_id: str = DEFAULT_APPLICATION_ID,
    profile: str = DEFAULT_PROFILE,
    user_scope: str | None = None,
) -> InstanceNames:
    """Return short, opaque, deterministic names for local IPC endpoints."""

    application_id = _validated_identifier(application_id, "application_id")
    profile = _validated_identifier(profile, "profile")
    scope = user_scope if user_scope is not None else current_user_scope()
    scope = _validated_identifier(scope, "user_scope")
    digest = hashlib.sha256(
        f"{application_id}\0{profile}\0{scope}".encode("utf-8")
    ).hexdigest()[:24]
    return InstanceNames(
        guardian=f"alpha-guard.guardian.{digest}",
        desktop=f"alpha-guard.desktop.{digest}",
    )


def current_user_scope() -> str:
    get_uid = getattr(os, "getuid", None)
    if get_uid is not None:
        return f"uid-{get_uid()}"
    # Windows local-socket access is additionally protected by a per-install
    # token.  The username is hashed by ``build_instance_names`` and is never
    # placed on the wire or in logs.
    return f"user-{getpass.getuser()}"


def _validated_identifier(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


__all__ = [
    "DEFAULT_APPLICATION_ID",
    "DEFAULT_PROFILE",
    "InstanceNames",
    "build_instance_names",
    "current_user_scope",
]
