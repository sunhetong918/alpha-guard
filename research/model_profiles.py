"""Model endpoint profiles: validation, identity and activation resolution."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .models import ModelProfile

MAX_PROFILES = 20


def normalize_endpoint(value: str, *, allow_private_network: bool) -> str:
    """Accept only an http(s) origin with an optional path, no query or fragment.

    Plain HTTP would send the bearer key in cleartext, so it is reserved for
    explicitly allowed private networks (local inference endpoints).
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("invalid_model_endpoint")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "%" in host or "\\" in value):
        raise ValueError("invalid_model_endpoint")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_model_endpoint") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid_model_endpoint")
    if not allow_private_network and parsed.scheme != "https":
        raise ValueError("https_required_for_public_endpoint")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("invalid_model_endpoint") from None
    authority = (f"[{host}]" if ":" in host else host) + (f":{port}" if port is not None else "")
    return f"{parsed.scheme}://{authority}{parsed.path.rstrip('/')}"


def build_profile(values: Mapping[str, Any], *, existing: ModelProfile | None = None) -> ModelProfile:
    """Validate a profile payload; key flags persist unless a new key is supplied."""
    allowed = {"id", "name", "protocol", "base_url", "model", "timeout_seconds",
               "allow_private_network", "enabled", "key_configured", "key_updated_at"}
    if set(values) - allowed:
        raise ValueError("unknown model profile fields")
    data = dict(values)
    identity = str(data.get("id") or "")
    if existing is None and identity:
        raise ValueError("model profile id is assigned by the application")
    if existing is not None and identity not in {"", existing.id}:
        raise ValueError("model profile id cannot be replaced")
    if existing is not None:
        data.setdefault("key_configured", existing.key_configured)
        data.setdefault("key_updated_at", existing.key_updated_at)
    data["id"] = existing.id if existing is not None else uuid.uuid4().hex
    allow_private = data.get("allow_private_network") is True
    data["base_url"] = normalize_endpoint(str(data.get("base_url", "")), allow_private_network=allow_private)
    data["updated_at"] = datetime.now(UTC).isoformat()
    return ModelProfile.model_validate(data)


def profile_public(profile: ModelProfile) -> dict[str, Any]:
    """A snapshot view; the API key never exists in this representation."""
    return profile.model_dump(mode="json")


def profile_identity(profile: ModelProfile) -> str:
    fingerprint = hashlib.sha256(profile.base_url.encode("utf-8")).hexdigest()[:12]
    return f"profile:{profile.protocol}:{fingerprint}:{profile.model}"


def active_model_profile(store: Any, settings: Any = None) -> ModelProfile | None:
    """Resolve the enabled profile named by the saved research settings."""
    del settings  # the activation pointer lives in the research store, not the environment
    try:
        pointer = store.load_settings().get("model_profile_id")
    except Exception:
        return None
    if not pointer:
        return None
    try:
        profile = store.get_model_profile(str(pointer))
    except Exception:
        return None
    if profile is None or not profile.enabled:
        return None
    return profile
