"""Local Guardian process primitives for Alpha Guard.

The package intentionally has no import-time dependency on a desktop toolkit.
The protocol, dispatcher, token store, and autostart helpers remain usable and
testable in the foundation CLI environment.  Qt is loaded only when the local
socket transport is started.
"""

from .application import GuardianApplication, GuardianBackend, GuardianDispatcher
from .instance import InstanceNames, build_instance_names
from .protocol import (
    GUARDIAN_METHODS,
    MAX_FRAME_BYTES,
    RpcErrorCode,
    RpcErrorKind,
    RpcErrorResponse,
    RpcRequest,
    RpcSuccessResponse,
)
from .token_store import GuardianTokenStore

__all__ = [
    "GUARDIAN_METHODS",
    "MAX_FRAME_BYTES",
    "GuardianApplication",
    "GuardianBackend",
    "GuardianDispatcher",
    "GuardianTokenStore",
    "InstanceNames",
    "RpcErrorCode",
    "RpcErrorKind",
    "RpcErrorResponse",
    "RpcRequest",
    "RpcSuccessResponse",
    "build_instance_names",
]
